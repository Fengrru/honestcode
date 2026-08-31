"""
Tool implementations for the HonestCode MCP server.

Each function is decorated with @mcp.tool() in server.py. They provide the
hallucination-detection capabilities exposed to MCP clients:
  - index_project        : build the project symbol index
  - load_project_deps    : load dependency APIs from requirements/pyproject
  - check_symbol         : verify a symbol is defined
  - check_api            : verify a library API call is correct
  - execute_code         : run code in a sandbox
  - scan_file            : scan a file for potential hallucinations
  - load_package_apis    : load a package's API signatures
  - get_project_stats    : index statistics
  - validate_types       : AST-based type checking
  - find_dead_code       : find potentially unused symbols
  - find_similar_code    : find similar/duplicate functions
  - explore_call_graph   : explore callers/callees of a symbol
  - search_code          : search source code with regex
"""

from __future__ import annotations

import ast
import logging
import os
import re
import sqlite3
import subprocess
import threading
from pathlib import Path

from honestcode.graph.graph_store import CallGraph, GraphCache
from honestcode.graph.watcher import ProjectWatcher
from honestcode.honest.router import HonestRouter
from honestcode.honest.symbol_index import ProjectIndex, get_project_index
from honestcode.mcp.knowledge_base import APIKnowledgeBase
from honestcode.sandbox import SandboxExecutor
from honestcode.structure.extractor import StructureExtractor
from honestcode.structure.relations import ParseResult
from honestcode.structure.utils import call_name
from honestcode.verify.evidence import build_report, render_text
from honestcode.verify.verify import valid_imported_names, verify_tree

__all__ = [
    "index_project",
    "load_project_deps",
    "check_symbol",
    "check_api",
    "execute_code",
    "scan_file",
    "load_package_apis",
    "get_project_stats",
    "validate_types",
    "find_dead_code",
    "find_similar_code",
    "explore_call_graph",
    "search_code",
    "choose_tool",
    "verify_file",
]

logger = logging.getLogger(__name__)

_AUTO_INDEX_ENABLED = os.environ.get("HONESTCODE_AUTO_INDEX", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_PROJECT_MARKERS = (".honestcode", ".git", "pyproject.toml", "setup.py", "requirements.txt")


def _find_project_root(start: Path) -> Path | None:
    """Walk upward from *start* looking for a project marker."""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".honestcode" / "config.json").exists():
            return candidate
        if (candidate / ".git").exists():
            return candidate
    for candidate in [cur, *cur.parents]:
        if any((candidate / m).exists() for m in _PROJECT_MARKERS):
            return candidate
    return None


def _ensure_index_for(path: Path) -> bool:
    """Auto-index the project containing *path* when no index is loaded."""
    global _project_index, _project_root
    with _state_lock:
        if _project_index is not None or not _AUTO_INDEX_ENABLED:
            return False
    root = _find_project_root(path)
    if root is None:
        return False
    idx = get_project_index(root, force_rebuild=False)
    with _state_lock:
        _project_index = idx
        _project_root = root
        _invalidate_graph_cache()
    return True


# ── Global server state ────────────────────────────────────────────────
_state_lock = threading.RLock()
_project_index: ProjectIndex | None = None
_project_root: Path | None = None
_dep_kb: APIKnowledgeBase = APIKnowledgeBase()
_sandbox: SandboxExecutor = SandboxExecutor()
_extractor: StructureExtractor = StructureExtractor()
# In-memory call-graph cache keyed by resolved project root. The SQLite
# GraphCache on disk is the source of truth; this is just a hot layer.
_graph_mem_cache: dict[str, CallGraph] = {}


def _cache_dir() -> Path:
    """Directory for symbol/graph caches (override with HONESTCODE_CACHE_DIR)."""
    return Path(os.environ.get("HONESTCODE_CACHE_DIR", os.path.expanduser("~/.cache/honestcode")))


# ── Internal helpers ───────────────────────────────────────────────────
def _module_name(p: Path, root: Path) -> str:
    """Infer a dotted module name from *p* relative to *root*.

    Only strips the ``src`` prefix when it is a standalone directory component
    (i.e. the standard ``src`` layout) and there are further path segments.
    """
    try:
        rel = p.resolve().relative_to(root.resolve())
    except ValueError:
        rel = p
    parts = list(rel.with_suffix("").parts)
    # Only strip "src" if it is a real layout prefix (has sub-packages after it).
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative_module(current_module: str, level: int, target: str) -> str:
    """Resolve a relative import target to an absolute module name."""
    if level == 0:
        return target
    parts = current_module.split(".")
    if level > len(parts):
        return ""
    prefix = parts[:-level]
    if target:
        prefix.append(target)
    return ".".join(prefix)


def _normalize_imported_symbols(
    imported_symbols: dict[str, str],
    tree: ast.AST,
    module: str,
) -> dict[str, str]:
    """Convert relative import sources to absolute module names.

    ``StructureExtractor`` stores relative imports without their leading dots.
    Normalizing them here lets ``valid_imported_names`` validate them against
    the project index without producing false positives on intra-package
    imports.
    """
    normalized = dict(imported_symbols)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            continue
        target = node.module or ""
        abs_module = _resolve_relative_module(module, node.level, target)
        if not abs_module:
            continue
        for alias in node.names:
            local = alias.asname or alias.name
            normalized[local] = f"{abs_module}.{alias.name}"
    return normalized


def _with_module_prefix(p: Path, root: Path, res: ParseResult) -> ParseResult:
    """Return a copy of *res* whose symbol names are prefixed by their module."""
    module = _module_name(p, root)
    if not module:
        return res
    prefix = module + "."

    def prefixed(d: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        return {prefix + k if not k.startswith(prefix) else k: v for k, v in d.items()}

    return ParseResult(
        edges=res.edges,
        func_defs=prefixed(res.func_defs),
        class_defs=prefixed(res.class_defs),
        var_defs=prefixed(res.var_defs),
        imports=res.imports,
        imported_symbols=res.imported_symbols,
        exported_symbols={
            prefix + s if not s.startswith(prefix) else s for s in (res.exported_symbols or set())
        },
    )


def _parse_results_for(root: Path) -> dict[Path, ParseResult]:
    """Parse every *.py file under root and return a path -> ParseResult map."""
    results: dict[Path, ParseResult] = {}
    for p in root.rglob("*.py"):
        if p.name.startswith(".") or "__pycache__" in p.parts:
            continue
        try:
            res = _extractor.parse_file(p)
            results[p] = _with_module_prefix(p, root, res)
        except Exception:  # noqa: BLE001
            continue
    return results


def _normalize_code(text: str) -> str:
    """Normalize code for similarity comparison."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(re.sub(r"\s+", " ", stripped))
    return "\n".join(out)


def _build_call_graph(root: Path) -> CallGraph:
    """Build a project-wide definition/reference graph from ASTs.

    Reference ``context`` is recorded as the **module-qualified** enclosing
    symbol name (e.g. ``pkg.core.main``) when derivable, so that callers of
    ``explore_call_graph`` can match by the same key they pass in. The short
    name (``main``) is still recorded as a secondary key for backwards
    compatibility with short-name lookups.
    """
    graph = CallGraph()

    for p, res in _parse_results_for(root).items():
        # Build a lineno -> module-qualified enclosing context map for this file.
        # Map from short name -> qualified name using the (already-prefixed)
        # function/class defs returned by _parse_results_for.
        module = _module_name(p, root)
        qualified_by_short: dict[str, str] = {}
        for qname in list(res.func_defs.keys()) + list(res.class_defs.keys()):
            short = qname.split(".")[-1]
            qualified_by_short.setdefault(short, qname)

        # Definitions
        for name, (start, end) in res.func_defs.items():
            graph.definitions.setdefault(name, []).append((p, start + 1, end + 1, "function"))
        for name, (start, end) in res.class_defs.items():
            graph.definitions.setdefault(name, []).append((p, start + 1, end + 1, "class"))
        for name, (start, end) in res.var_defs.items():
            graph.definitions.setdefault(name, []).append((p, start + 1, end + 1, "variable"))

        # AST-level references with enclosing context (module-qualified).
        # The extractor's same-scope edges are intentionally not re-recorded
        # here: the AST walk below already captures every Name/Attribute/
        # Call reference (with context), and re-adding edges would duplicate
        # caller entries with wrong (definition) line numbers.
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError:
            continue

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                ctx = qualified_by_short.get(node.name, node.name)
                _record_reference(node, p, graph, ctx)
                _collect_refs_in(node, p, graph, ctx, qualified_by_short)
            else:
                _record_reference(node, p, graph, module or None)
                _collect_refs_in(node, p, graph, module or None, qualified_by_short)

    return graph


def _collect_refs_in(
    node: ast.AST,
    p: Path,
    graph: CallGraph,
    context: str | None,
    qualified_by_short: dict[str, str] | None = None,
) -> None:
    """Collect call/name references inside *node*, attaching *context*.

    When *qualified_by_short* is provided and a nested ``FunctionDef`` /
    ``ClassDef`` is encountered, the context is updated to that node's
    module-qualified name (looked up by its short name) so that references
    inside a class method are attributed to ``pkg.Mod.Class.method`` rather
    than to the enclosing class.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Recurse with the child's qualified name as the new context.
            if qualified_by_short is not None:
                child_ctx = qualified_by_short.get(child.name)
                if child_ctx is None:
                    # Build a dotted path from the current context.
                    child_ctx = f"{context}.{child.name}" if context else child.name
            else:
                child_ctx = child.name
            _record_reference(child, p, graph, child_ctx)
            _collect_refs_in(child, p, graph, child_ctx, qualified_by_short)
        else:
            _record_reference(child, p, graph, context)
            _collect_refs_in(child, p, graph, context, qualified_by_short)


def _record_reference(
    child: ast.AST,
    p: Path,
    graph: CallGraph,
    context: str | None,
) -> None:
    """Record a single AST node as a reference if it is a call/name/attribute."""
    if isinstance(child, ast.Call):
        name = call_name(child.func)
        if name:
            graph.references.setdefault(name, []).append((p, child.lineno, "call", context))
    elif isinstance(child, ast.Name):
        if isinstance(child.ctx, ast.Store):
            return
        graph.references.setdefault(child.id, []).append((p, child.lineno, "reference", context))
    elif isinstance(child, ast.Attribute):
        if isinstance(child.ctx, ast.Store):
            return
        name = call_name(child)
        if name:
            graph.references.setdefault(name, []).append((p, child.lineno, "attribute", context))


# ── Tools ──────────────────────────────────────────────────────────────
def _invalidate_graph_cache() -> None:
    """Drop the in-memory call-graph cache (after re-indexing)."""
    with _state_lock:
        _graph_mem_cache.clear()


def _get_call_graph(root: Path) -> CallGraph:
    """Return a fresh call graph, reading from the SQLite cache when possible.

    Falls back to a full AST rebuild and persists it to disk, so repeated graph
    queries (call-graph, dead-code, impact, affected) stay fast on large repos.
    """
    with _state_lock:
        cached = _graph_mem_cache.get(str(root))
    if cached is not None:
        return cached

    cache = GraphCache.for_root(root, _cache_dir())
    if cache.is_fresh(root):
        loaded = cache.load()
        if loaded is not None:
            with _state_lock:
                _graph_mem_cache[str(root)] = loaded
            return loaded

    graph = _build_call_graph(root)
    try:
        cache.save(graph, root)
    except OSError:
        logger.debug("Could not persist graph cache for %s", root)
    with _state_lock:
        _graph_mem_cache[str(root)] = graph
    return graph


_watcher: ProjectWatcher | None = None


def _ensure_watcher(root: Path) -> None:
    """Start a background file watcher that auto-reindexes on changes."""
    global _watcher
    if _watcher is not None:
        return

    def _on_change(changed: list) -> None:
        global _project_index, _project_root
        idx = get_project_index(root, force_rebuild=False)
        with _state_lock:
            _project_index = idx
            _project_root = root
            _invalidate_graph_cache()
        logger.info("Watcher: re-indexed after %d file changes", len(changed))

    _watcher = ProjectWatcher(root, _on_change).start()


def stop_watching() -> dict:
    """Stop the background file watcher, if any."""
    global _watcher
    with _state_lock:
        if _watcher is None:
            return {"success": True, "watching": False}
        _watcher.stop()
        _watcher = None
    return {"success": True, "watching": False}


def index_project(root_path: str, force_rebuild: bool = False, watch: bool = False) -> dict:
    """Build (or reuse) the symbol index for a project directory."""
    global _project_index, _project_root
    root = Path(root_path)
    if not root.exists():
        return {"success": False, "error": f"Path does not exist: {root_path}"}
    resolved = root.resolve()
    idx = get_project_index(resolved, force_rebuild=force_rebuild)
    with _state_lock:
        _project_root = resolved
        _project_index = idx
        _invalidate_graph_cache()
    if watch:
        _ensure_watcher(resolved)
    return {
        "success": True,
        "symbols_indexed": len(idx.symbols),
        "root": str(resolved),
        "cached": idx.cached,
        "watching": _watcher is not None,
    }


def load_project_deps(root_path: str) -> dict:
    """Load dependency APIs declared in requirements.txt / pyproject.toml."""
    root = Path(root_path)
    loaded: list[str] = []
    for candidate in ("requirements.txt", "pyproject.toml"):
        f = root / candidate
        if not f.exists():
            continue
        pkgs = _parse_requires(f)
        for pkg in pkgs:
            try:
                n = _dep_kb.load_package(pkg)
                if n:
                    loaded.append(pkg)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to load package %s", pkg)
    return {"success": True, "packages_loaded": loaded, "total_apis": len(_dep_kb.all_names())}


def _parse_requires(f: Path) -> list[str]:
    text = f.read_text(encoding="utf-8")
    pkgs: list[str] = []
    if f.name == "requirements.txt":
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Skip pip options / VCS installs.
            if line.startswith("-") or line.startswith("http://") or line.startswith("https://"):
                continue
            m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
            if m:
                pkgs.append(m.group(1))
    else:  # pyproject.toml
        pkgs.extend(_parse_pyproject_deps(text))
    return pkgs


def _parse_pyproject_deps(text: str) -> list[str]:
    """Extract package names from pyproject.toml, preferring TOML parsing."""
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(text)
        deps: list[str] = []
        for key in ("project.dependencies", "tool.poetry.dependencies"):
            parts = key.split(".")
            cur = data
            for part in parts:
                cur = cur.get(part, {})
            if isinstance(cur, list):
                deps.extend(_strip_version_spec(d) for d in cur if isinstance(d, str))
            elif isinstance(cur, dict):
                # Poetry declares dependencies as {name: version} pairs.
                deps.extend(k for k in cur if isinstance(k, str))
        return deps
    except Exception:  # noqa: BLE001
        pass

    # Fallback regex for quoted / unquoted declarations.
    pkgs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "name>=1.0" or name = ">=1.0"
        m = re.match(r'^["\']?([A-Za-z0-9_.\-]+)["\']?\s*(?:[<>=!~^]=?|$|\s)', line)
        if m:
            pkgs.append(m.group(1))
    return pkgs


def _strip_version_spec(dep: str) -> str:
    """Return the package name part of a dependency specifier."""
    dep = dep.strip()
    m = re.match(r"^([A-Za-z0-9_.\-]+)", dep)
    return m.group(1) if m else dep


def check_symbol(symbol_name: str, file_path: str | None = None) -> dict:
    """Check whether a symbol is defined in the project."""
    with _state_lock:
        idx = _project_index
    if idx is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}
    info = idx.symbols.get(symbol_name)
    return {
        "success": True,
        "symbol": symbol_name,
        "defined": info is not None,
        "location": {"file": info.file, "line": info.line} if info else None,
    }


def check_api(api_name: str) -> dict:
    """Verify a library API call is correct (e.g. pd.read_exel -> read_excel)."""
    if _dep_kb.has(api_name):
        return {"success": True, "api": api_name, "valid": True}

    base = api_name.split(".")[0]
    dep_names = _dep_kb.all_names()
    base_known = _dep_kb.has(base) or any(k.startswith(base + ".") for k in dep_names)
    if not base_known:
        return {
            "success": True,
            "api": api_name,
            "valid": False,
            "reason": "unknown module",
            "suggestion": [],
        }

    members = [k for k in dep_names if k.startswith(base + ".")]
    attr_part = api_name.split(".")[-1]
    exact = [m for m in members if m == api_name]
    if exact:
        return {"success": True, "api": api_name, "valid": True}

    close = [m for m in members if m.split(".")[-1] == attr_part]
    if close:
        return {
            "success": True,
            "api": api_name,
            "valid": False,
            "reason": "Attribute not found on module",
            "suggestion": close[:3],
        }

    from difflib import get_close_matches

    fuzzy = get_close_matches(api_name, members, n=3, cutoff=0.6)
    return {
        "success": True,
        "api": api_name,
        "valid": False,
        "reason": "Attribute not found on module",
        "suggestion": fuzzy if fuzzy else members[:3],
    }


def execute_code(code: str, prelude: str = "", known_names: list[str] | None = None) -> dict:
    """Execute code in a sandboxed subprocess and return structured results."""
    result = _sandbox.execute(code=code, prelude=prelude, known_names=set(known_names or []))
    return result.to_dict()


def _collect_local_defined(tree: ast.AST) -> set[str]:
    """Collect locally defined names (functions, classes, vars, parameters).

    Includes parameters and assignment targets so that ``self.helper()`` or
    ``client.fetch()`` are not flagged when ``self`` / ``client`` are bound
    in the enclosing scope.
    """
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            for a in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if a.arg:
                    defined.add(a.arg)
            if node.args.vararg:
                defined.add(node.args.vararg.arg)
            if node.args.kwarg:
                defined.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_assign_targets(target, defined)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect_assign_targets(node.target, defined)
        elif isinstance(node, ast.NamedExpr):  # walrus :=
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
    return defined


def _collect_assign_targets(target: ast.expr, defined: set[str]) -> None:
    if isinstance(target, ast.Name):
        defined.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_assign_targets(elt, defined)
    elif isinstance(target, ast.Starred):
        _collect_assign_targets(target.value, defined)


def _builtin_names() -> set[str]:
    """Return the full set of Python builtin names (functions + exceptions)."""
    import builtins

    return set(dir(builtins))


def _scan_non_python(p: Path) -> dict:
    """Scan a non-Python file via the optional tree-sitter multi-language path."""
    from honestcode.structure import multi_lang

    lang = multi_lang.detect_language(p)
    if lang is None:
        return {
            "success": False,
            "error": f"Unsupported language for {p.suffix or 'file'}. "
            "Python is fully supported; other languages need "
            "'pip install honestcode[multi-language]'.",
        }
    try:
        symbols = multi_lang.extract_symbols(p)
    except multi_lang.TreeSitterUnavailable as exc:
        return {
            "success": False,
            "error": f"{exc}. Language detected: {lang}. Run: pip install -e '.[multi-language]'",
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"multi-language scan failed: {exc}"}

    defined = {s["name"] for s in symbols}
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"success": False, "error": f"cannot read file: {exc}"}

    issues: list[dict] = []
    # Negative lookbehind skips attribute calls (``obj.method(``), which
    # cannot be judged statically; keyword set covers language constructs
    # that look like calls (``new Foo(``, ``require(``, ...).
    for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", source):
        name = m.group(1)
        if name in defined or name in {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "function",
            "new",
            "typeof",
            "return",
        }:
            continue
        line = source[: m.start()].count("\n") + 1
        issues.append({"type": "undefined_call", "name": name, "line": line})

    return {
        "success": True,
        "file": str(p),
        "language": lang,
        "issues": issues,
        "defined_symbols": len(defined),
    }


def scan_file(file_path: str) -> dict:
    """Scan a file for potential hallucinations: undefined symbols and invented APIs."""
    p = Path(file_path)
    if not p.exists():
        return {"success": False, "error": f"File not found: {file_path}"}
    if p.suffix.lower() != ".py":
        return _scan_non_python(p)
    try:
        res = _extractor.parse_file(p)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

    # Try to ground the file automatically: most agents call scan_file on a
    # freshly-written path without remembering to call index_project first.
    _ensure_index_for(p)

    with _state_lock:
        idx = _project_index
        dep_names = _dep_kb.all_names()

    try:
        source = p.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(p))
    except SyntaxError as e:
        return {"success": False, "error": f"SyntaxError: {e}"}

    # Locally defined names (functions/classes/vars + parameters + loop targets).
    defined = _collect_local_defined(tree)
    # Module-qualified project symbols.
    if idx is not None:
        defined |= set(idx.symbols.keys())
    # Names introduced by import statements, validated against the index and
    # the loaded dependency API knowledge base.
    module = _module_name(p, Path(idx.root)) if idx is not None else p.stem
    imported_symbols = _normalize_imported_symbols(res.imported_symbols or {}, tree, module)
    defined |= valid_imported_names(imported_symbols, idx, dep_names)
    # Dependency API names.
    defined |= dep_names
    # Full builtin set instead of a hand-picked subset.
    defined |= _builtin_names()

    findings, checked = verify_tree(
        tree,
        path=p,
        root=Path(idx.root) if idx is not None else None,
        index=idx,
        defined=defined,
        dep_names=dep_names,
        imported_symbols=imported_symbols,
    )
    report = build_report(file=str(p), findings=findings, checked=checked)
    report["defined_symbols"] = len(defined)
    return report


def verify_file(file_path: str) -> dict:
    """Verify a file and return structured, agent-readable evidence.

    Unlike ``scan_file`` (which also returns legacy ``issues``), this tool
    exposes only the agent-facing protocol: ``status``, ``findings`` with
    ``evidence``, and a human-readable ``text`` summary.
    """
    report = scan_file(file_path)
    if not report.get("success", True):
        return report
    return {
        "success": True,
        "status": report["status"],
        "file": report["file"],
        "findings": report["findings"],
        "summary": report["summary"],
        "text": render_text(report),
    }


def load_package_apis(package_name: str) -> dict:
    """Load (and cache) API signatures for a specific package."""
    count = _dep_kb.load_package(package_name)
    return {"success": True, "package": package_name, "api_count": count}


def get_project_stats() -> dict:
    """Return statistics about the current index."""
    with _state_lock:
        idx = _project_index
    if idx is None:
        return {"success": False, "error": "Project not indexed."}
    return {
        "success": True,
        "symbols": len(idx.symbols),
        "dependency_apis": len(_dep_kb.all_names()),
        "indexed": idx is not None,
    }


def validate_types(code: str) -> dict:
    """Run a lightweight AST-based type check to catch obvious mismatches."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"success": False, "error": f"SyntaxError: {e}"}

    issues: list[dict] = []

    for node in ast.walk(tree):
        # 1. None iteration
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            _check_none_iterable(node.iter, issues)
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In):
                    _check_none_iterable(comparator, issues)

        # 2. Calling a constant literal
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Constant):
            issues.append(
                {
                    "type": "non_callable",
                    "message": "Attempting to call a constant value",
                    "line": node.lineno,
                }
            )

        # 3. String method on non-string constant
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"upper", "lower", "strip", "split", "join", "startswith", "endswith"}
            and isinstance(node.value, ast.Constant)
            and not isinstance(node.value.value, str)
        ):
            issues.append(
                {
                    "type": "type_mismatch",
                    "message": f"'{node.attr}' called on non-string constant",
                    "line": node.lineno,
                }
            )

        # 4. Common builtins with wrong argument count
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            _check_builtin_argc(node, issues)

    return {"success": True, "issues": issues, "note": "AST structural check complete"}


def _check_none_iterable(node: ast.expr, issues: list[dict]) -> None:
    """Flag iterating over a None constant or expressions likely to return None.

    Detects:
    - Explicit ``None`` literal
    - ``dict.get(...)`` / ``dict.pop(...)`` without a default value
    - Call to functions that commonly return None (e.g. ``os.environ.get``)
    """
    if isinstance(node, ast.Constant) and node.value is None:
        issues.append(
            {
                "type": "none_iteration",
                "message": "Attempting to iterate over None",
                "line": getattr(node, "lineno", 0),
            }
        )
        return

    # dict.get(key) without default returns None.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if (
            method in ("get", "pop")
            and len(node.args) < 2
            and not any(kw.arg == "default" for kw in node.keywords)
        ):
            issues.append(
                {
                    "type": "none_iteration",
                    "message": f"'{method}' without default may return None",
                    "line": getattr(node, "lineno", 0),
                }
            )


def _check_builtin_argc(node: ast.Call, issues: list[dict]) -> None:
    """Check argument counts for a small set of common builtins.

    Rules are (min_positional, max_positional, allowed_keyword_names).
    ``None`` means unbounded.
    """
    name = node.func.id
    args = len(node.args)
    kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}  # exclude **kwargs
    rules = {
        "len": (1, 1, set()),
        "repr": (1, 1, set()),
        "str": (0, 1, {"encoding", "errors"}),
        "int": (0, 2, {"base"}),
        "float": (0, 1, set()),
        "bool": (0, 1, set()),
        "abs": (1, 1, set()),
        "round": (1, 2, {"ndigits"}),
        "sum": (1, 2, {"start"}),
        "min": (1, None, set()),
        "max": (1, None, set()),
        "sorted": (1, 3, {"key", "reverse"}),
        "enumerate": (1, 2, {"start"}),
        "zip": (0, None, {"strict"}),
        "map": (2, None, set()),
        "filter": (2, None, set()),
    }
    if name not in rules:
        return
    min_args, max_args, allowed_kw = rules[name]

    invalid_kw = kwargs - allowed_kw
    if invalid_kw:
        issues.append(
            {
                "type": "arg_count",
                "message": f"'{name}' does not accept keyword argument(s): {', '.join(sorted(invalid_kw))}",
                "line": node.lineno,
            }
        )
        return

    if args < min_args:
        issues.append(
            {
                "type": "arg_count",
                "message": f"'{name}' expects at least {min_args} positional argument(s)",
                "line": node.lineno,
            }
        )
    elif max_args is not None and args > max_args:
        issues.append(
            {
                "type": "arg_count",
                "message": f"'{name}' expects at most {max_args} positional argument(s)",
                "line": node.lineno,
            }
        )


def find_dead_code(
    entrypoints: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    include_tests: bool = True,
) -> dict:
    """Find symbols that appear to be unused in the project."""
    with _state_lock:
        root = _project_root
        idx = _project_index
    if root is None or idx is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    ignore_res = [re.compile(p) for p in (ignore_patterns or [])]
    graph = _get_call_graph(root)

    alive: set[str] = set(entrypoints or [])

    # Symbols referenced anywhere in the project are considered alive.
    for name in graph.references:
        if name in graph.definitions:
            alive.add(name)

    if include_tests:
        for name, locations in graph.definitions.items():
            for p, _line, _end, _kind in locations:
                if "test" in p.parts or p.name.startswith("test_"):
                    alive.add(name)
                    break

    # Treat dunder hooks and public __init__ exports as alive.
    for name in graph.definitions:
        if name.startswith("__") and name.endswith("__"):
            alive.add(name)

    dead: list[dict] = []
    for name, locations in graph.definitions.items():
        if name in alive:
            continue
        filtered = [
            (str(p), line, kind)
            for p, line, _end, kind in locations
            if not any(r.search(str(p)) for r in ignore_res)
        ]
        if filtered:
            dead.append({"symbol": name, "locations": filtered})

    return {"success": True, "dead_symbols": dead, "count": len(dead)}


def find_similar_code(threshold: float = 0.85) -> dict:
    """Find function-level code clones across the project.

    Uses a length-ratio pre-filter to skip obviously dissimilar pairs before
    running the more expensive ``SequenceMatcher``.
    """
    with _state_lock:
        root = _project_root
    if root is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    functions: list[tuple[Path, str, str, tuple[int, int]]] = []
    for p, res in _parse_results_for(root).items():
        code = p.read_text(encoding="utf-8")
        for name, (start, end) in res.func_defs.items():
            lines = code.splitlines()[start : end + 1]
            body = _normalize_code("\n".join(lines))
            functions.append((p, name, body, (start + 1, end + 1)))

    from difflib import SequenceMatcher

    # Pre-filter: group by similar length to avoid comparing very short or
    # very different functions.  The similarity ratio between two strings is
    # bounded by min(len(a), len(b)) / max(len(a), len(b)), so we can skip
    # pairs whose length ratio is already below the threshold.
    pairs: list[dict] = []
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for i, (p1, n1, b1, r1) in enumerate(functions):
        len1 = len(b1)
        if len1 < 30:
            continue
        for j in range(i + 1, len(functions)):
            p2, n2, b2, r2 = functions[j]
            len2 = len(b2)
            if len2 < 30:
                continue
            # Length-ratio bound: if the shorter is less than threshold * longer, skip.
            shorter, longer = (len1, len2) if len1 <= len2 else (len2, len1)
            if shorter < threshold * longer:
                continue
            key = tuple(sorted([(str(p1), n1), (str(p2), n2)]))
            if key in seen:
                continue
            ratio = SequenceMatcher(None, b1, b2).ratio()
            if ratio >= threshold:
                seen.add(key)
                pairs.append(
                    {
                        "symbol_a": {"file": str(p1), "name": n1, "lines": r1},
                        "symbol_b": {"file": str(p2), "name": n2, "lines": r2},
                        "similarity": round(ratio, 3),
                    }
                )

    pairs.sort(key=lambda x: x["similarity"], reverse=True)
    return {"success": True, "pairs": pairs, "count": len(pairs)}


def explore_call_graph(symbol_name: str) -> dict:
    """Explore callers and callees of a symbol across the project."""
    with _state_lock:
        root = _project_root
    if root is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    graph = _get_call_graph(root)

    definitions = graph.definitions.get(symbol_name, [])
    short_name = symbol_name.split(".")[-1]

    # Names that count as "this symbol" — both the fully-qualified form and
    # the short form, so a call context recorded as either will match.
    self_names = {symbol_name, short_name}

    # Callers: places that reference *symbol_name* (or its short name).
    callers: list[dict] = []
    seen_callers: set[tuple[str, int, str]] = set()
    for ref_name, refs in graph.references.items():
        if ref_name != symbol_name and ref_name != short_name:
            continue
        for p, line, kind, context in refs:
            key = (str(p), line, context or "")
            if key in seen_callers:
                continue
            seen_callers.add(key)
            callers.append(
                {
                    "caller": context or "<module>",
                    "file": str(p),
                    "line": line,
                    "kind": kind,
                }
            )

    # Callees: symbols referenced from inside any of *symbol_name*'s definitions.
    # A reference is "inside" when its enclosing context matches the symbol
    # (by qualified or short name) AND it lives in the same file AND its line
    # falls within the def's span (short names can collide across modules).
    def_spans = [(p, line, end) for p, line, end, _kind in definitions]
    callee_names: set[str] = set()
    for ref_name, refs in graph.references.items():
        for rp, rline, _rkind, rcontext in refs:
            if rcontext not in self_names:
                continue
            if any(fp == rp and start <= rline <= end for fp, start, end in def_spans):
                callee_names.add(ref_name)

    callees: list[dict] = []
    for callee in sorted(callee_names):
        locations = graph.definitions.get(callee, [])
        callees.append(
            {
                "callee": callee,
                "defined": [
                    {"file": str(p), "line": line, "kind": kind}
                    for p, line, _end, kind in locations
                ],
            }
        )

    result: dict = {
        "success": True,
        "symbol": symbol_name,
        "definitions": [
            {"file": str(p), "line": line, "kind": kind} for p, line, _end, kind in definitions
        ],
        "callers": callers,
        "callees": callees,
    }
    # Blast-radius summary: how far a change to this symbol would ripple.
    if definitions:
        impact = explore_impact(symbol_name, max_depth=3)
        if impact.get("success"):
            result["impact"] = impact["counts"]
    return result


def explore_impact(symbol_name: str, max_depth: int = 3) -> dict:
    """Compute the blast radius of a symbol: transitively impacted symbols/files.

    Walks the call graph in both directions (callers and callees) up to
    ``max_depth`` hops and summarizes which symbols and files would be affected
    if ``symbol_name`` changed.
    """
    with _state_lock:
        root = _project_root
    if root is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    graph = _get_call_graph(root)
    if symbol_name not in graph.definitions:
        return {"success": False, "error": f"Symbol not found in project: {symbol_name}"}

    def _callers(name: str) -> set[str]:
        """Symbols whose bodies reference *name* (or its short form)."""
        short = name.split(".")[-1]
        found: set[str] = set()
        for ref_name, refs in graph.references.items():
            if ref_name != name and ref_name != short:
                continue
            for _p, _line, _kind, context in refs:
                if context:
                    found.add(context)
        return found

    def _callees(name: str) -> set[str]:
        """Symbols referenced from inside *name*'s definitions (same file)."""
        spans = [(p, line, end) for p, line, end, _kind in graph.definitions.get(name, [])]
        if not spans:
            return set()
        short = name.split(".")[-1]
        found: set[str] = set()
        for ref_name, refs in graph.references.items():
            for rp, rline, _rkind, rcontext in refs:
                if rcontext != name and rcontext != short:
                    continue
                if any(fp == rp and start <= rline <= end for fp, start, end in spans):
                    found.add(ref_name)
        return found

    visited: set[str] = set()
    frontier: set[str] = {symbol_name}
    depth_map: dict[str, int] = {}
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: set[str] = set()
        for name in frontier:
            if name in visited:
                continue
            visited.add(name)
            depth_map[name] = depth
            nxt.update(_callers(name))
            nxt.update(_callees(name))
        frontier = nxt - visited

    impacted_files: set[str] = set()
    for name in visited:
        for p, _line, _end, _kind in graph.definitions.get(name, []):
            impacted_files.add(str(p))

    return {
        "success": True,
        "symbol": symbol_name,
        "max_depth": max_depth,
        "impacted_symbols": sorted(visited),
        "impacted_files": sorted(impacted_files),
        "counts": {
            "symbols": len(visited),
            "files": len(impacted_files),
            "depth": max(depth_map.values(), default=0),
        },
    }


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def affected_files(base: str = "HEAD", head: str | None = None, max_depth: int = 4) -> dict:
    """Find files (especially tests) that may be affected by a git diff.

    ``base`` (default ``HEAD``) is diffed against ``head`` when given, else the
    working tree. Changed files' defined symbols are traced backwards through
    the call graph so callers-of-callers up to ``max_depth`` hops are included.
    """
    with _state_lock:
        root = _project_root
    if root is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    if head:
        proc = _run_git(root, "diff", "--name-only", base, head)
    else:
        proc = _run_git(root, "diff", "--name-only", base)
    if proc is None or proc.returncode != 0:
        err = (proc.stderr if proc else "git unavailable").strip()
        return {"success": False, "error": f"git diff failed: {err}"}
    changed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    graph = _get_call_graph(root)

    # Symbols defined in changed files (paths are relative to the repo root).
    # git always emits forward slashes; normalize Windows paths to match.
    seeds: set[str] = set()
    changed_set = {c.replace("\\", "/") for c in changed}
    for name, locs in graph.definitions.items():
        for p, _line, _end, _kind in locs:
            try:
                rel = str(p.relative_to(root)).replace("\\", "/")
            except ValueError:
                rel = str(p)
            if rel in changed_set:
                seeds.add(name)
    if not seeds:
        return {
            "success": True,
            "changed": changed,
            "affected_files": [],
            "affected_tests": [],
            "propagation": 0,
        }

    # Reverse BFS (bounded by max_depth): who references these symbols,
    # transitively. Depth counts hops from the seed symbols.
    visited: set[str] = set()
    frontier: set[str] = set(seeds)
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: set[str] = set()
        for name in frontier:
            if name in visited:
                continue
            visited.add(name)
            short = name.split(".")[-1]
            for ref_name, refs in graph.references.items():
                if ref_name != name and ref_name != short:
                    continue
                for _p, _line, _kind, context in refs:
                    if context and context not in visited:
                        nxt.add(context)
        frontier = nxt - visited
        if len(visited) > 5000:  # safety valve for pathological graphs
            break

    affected: set[str] = set()
    for name in visited:
        for p, _line, _end, _kind in graph.definitions.get(name, []):
            affected.add(str(p))

    affected_tests = sorted(f for f in affected if "test" in f.lower())
    return {
        "success": True,
        "changed": changed,
        "affected_files": sorted(affected),
        "affected_tests": affected_tests,
        "propagation": len(visited) - len(seeds),
    }


def search_code(pattern: str, glob: str = "*.py") -> dict:
    """Search project source code with a regex pattern."""
    with _state_lock:
        root = _project_root
    if root is None:
        return {"success": False, "error": "Project not indexed. Call index_project first."}

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return {"success": False, "error": f"Invalid regex: {e}"}

    # FTS5 acceleration: for plain .py searches, shrink the scan to a superset
    # of candidate files via the full-text index, then run the exact regex only
    # on those. Results are identical to a full scan, but large repos touch
    # far fewer files. Falls back to a full scan when FTS5 is unavailable.
    candidates: list[Path] | None = None
    if glob == "*.py":
        try:
            cache = GraphCache.for_root(root, _cache_dir())
            if not cache.is_fresh(root):
                # First search after a change: (re)build the graph + FTS5 index
                # once, then every later search hits the index.
                _get_call_graph(root)
            if cache.is_fresh(root):
                rel_candidates = cache.search_candidate_files(pattern)
                if rel_candidates is not None:
                    if not rel_candidates:
                        return {
                            "success": True,
                            "pattern": pattern,
                            "matches": [],
                            "count": 0,
                            "engine": "fts5",
                        }
                    candidates = [root / rel for rel in rel_candidates]
        except (OSError, sqlite3.Error):
            candidates = None

    matches: list[dict] = []
    for p in candidates if candidates is not None else root.rglob(glob):
        if "__pycache__" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for m in regex.finditer(text):
            line = text[: m.start()].count("\n") + 1
            matches.append(
                {
                    "file": str(p),
                    "line": line,
                    "match": m.group(0)[:200],
                }
            )

    return {
        "success": True,
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "engine": "fts5" if candidates is not None else "regex",
    }


def choose_tool(query: str) -> dict:
    """Choose the most appropriate tool for a natural-language query."""
    choice = HonestRouter.choose_tool(query)
    return {
        "success": True,
        "tool": choice.intent.value,
        "confidence": round(choice.confidence, 3),
        "reason": choice.reason,
    }
