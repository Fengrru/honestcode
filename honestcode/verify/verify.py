"""Ground-truth verification of a single Python file.

This is the loop HonestCode actually sells:

    coding agent writes code → verify against the repository → evidence → fix

The verifier walks the AST of the file the agent just produced and, for every
call site, tries to *prove* it is grounded in the repository. When it cannot
prove grounding it emits a :class:`~honestcode.verify.evidence.Finding` that
carries the fix (`did_you_mean`, `available_methods`, `expected` / `actual`).

Design rule: **silence beats noise**. A call is reported only when the
verifier resolved the receiver to a concrete class whose member surface it
could fully reconstruct *and* proved the member absent. Unresolved bases,
``__getattr__`` hooks, ``*args``/``**kwargs`` unpacking, dynamically
decorated callables, and arities that keyword arguments could satisfy all
cause the check to be skipped rather than guessed — a false positive costs an
agent a wrong edit, which is worse than a missed one.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any

from honestcode.structure.extractor import StructureExtractor
from honestcode.structure.utils import (
    call_name,
    module_name_for,
    stdlib_resolves,
    stdlib_roots,
)
from honestcode.verify.evidence import (
    ACTION_INSPECT,
    CONFIDENCE_DETERMINISTIC,
    KIND_INVENTED_API,
    KIND_SYNTAX_ERROR,
    KIND_UNDEFINED_SYMBOL,
    KIND_WRONG_CALL,
    Finding,
)
from honestcode.verify.surface import (
    EMPTY_BASES,
    ClassSurface,
    MethodInfo,
    annotation_name,
    extract_file_surfaces,
    load_file_facts,
)

if TYPE_CHECKING:
    from honestcode.honest.symbol_index import ProjectIndex

__all__ = ["SurfaceStore", "verify_tree", "verify_file_source", "valid_imported_names"]

MAX_EVIDENCE_METHODS = 8
_NEAR_MISS_CUTOFF = 0.6
_MAX_BASE_DEPTH = 5


def valid_imported_names(
    imported_symbols: dict[str, str],
    index: ProjectIndex | None,
    dep_names: set[str],
    dep_roots: set[str] | None = None,
) -> set[str]:
    """Return imported names that resolve to a known project or dependency symbol.

    Without this filter, ``from myproject import does_not_exist`` is treated as
    a defined name and the agent's hallucinated call is silently accepted. We
    keep a name only when:

    * the source symbol exists in the project index,
    * the source is a known project module (``import lib.api``),
    * the name is re-exported by a project module (``from .compat import
      urlparse`` where ``compat`` itself imports it),
    * the source root is a known dependency root (``import numpy as np``), or
    * the full source is a known dependency API (``from math import sqrt``).

    Standard-library sources get one extra guarantee: they are real, importable
    code, so the attribute is verified against the live module — ``from typing
    import TypeVarr`` is rejected while ``from typing import TypeVar`` passes.
    Third-party roots are trusted wholesale when they were merely declared
    (they may not be installed, so the name cannot be checked) — *dep_roots*
    supplies those root names; when omitted they are derived from *dep_names*.
    """
    valid: set[str] = set()
    roots = dep_roots if dep_roots is not None else {n.split(".", 1)[0] for n in dep_names}
    stdlib = stdlib_roots()
    if index is None:
        # No project index: fall back to the old trusting behaviour.
        for local_name, source_name in imported_symbols.items():
            valid.add(local_name)
            if source_name:
                valid.add(source_name.split(".")[0])
        return valid

    project_roots: set[str] = set()
    project_modules: set[str] = set()
    for qname in index.symbols:
        parts = qname.split(".")
        project_roots.add(parts[0])
        for i in range(1, len(parts)):
            project_modules.add(".".join(parts[:i]))

    for local_name, source_name in imported_symbols.items():
        if not source_name:
            valid.add(local_name)
            continue

        root = source_name.split(".")[0]

        # Project-grounded first, so a project that shadows a stdlib or
        # dependency name still resolves through the index.
        if source_name in index.symbols or source_name in project_modules:
            valid.add(local_name)
            continue
        mod, _, attr = source_name.rpartition(".")
        if attr and mod and attr in index.module_symbols.get(mod, ()):
            # Re-export through a project module: ``from .compat import
            # urlparse`` where compat.py itself imports it. The name is not a
            # symbol of that module (it is an import there) but it is bound at
            # its top level, so the importing file is grounded.
            valid.add(local_name)
            continue

        if root in roots:
            valid.add(root)
            if root in stdlib and "." in source_name and stdlib_resolves(source_name) is False:
                # The stdlib module imports fine but lacks this attribute —
                # an invented standard-library symbol.
                continue
            valid.add(local_name)
            continue
        if source_name in dep_names:
            valid.add(local_name)
            continue
        if root in project_roots:
            # ``from lib.api import does_not_exist`` keeps ``lib`` bound but
            # rejects the invented local name.
            valid.add(root)

    return valid


# ── Name resolution ─────────────────────────────────────────────────────
class NameResolver:
    """Resolve an identifier used in one file to a module-qualified name.

    Resolution is purely syntactic (import map → same module → unique global
    suffix match), so it never imports user code and never guesses.
    """

    def __init__(self, module: str, imported: dict[str, str], known: set[str]):
        self.module = module
        self.imported = imported
        self._known = known
        self._by_short: dict[str, list[str]] | None = None

    def resolve(self, name: str) -> str | None:
        src = self.imported.get(name)
        if src:
            for candidate in (src, src.lstrip(".")):
                if candidate in self._known:
                    return candidate
            if self.module:
                joined = f"{self.module}.{src.lstrip('.')}"
                if joined in self._known:
                    return joined
        if self.module and f"{self.module}.{name}" in self._known:
            return f"{self.module}.{name}"
        if name in self._known:
            return name
        hits = self._suffix_hits(name)
        return hits[0] if len(hits) == 1 else None

    def resolve_dotted(self, name: str) -> str | None:
        """Resolve ``client.UserClient`` by resolving its head component."""
        if not name:
            return None
        head, _, rest = name.partition(".")
        target = self.resolve(head)
        if target is None:
            return None
        return f"{target}.{rest}" if rest else target

    def _suffix_hits(self, name: str) -> list[str]:
        """Globally unique short-name fallback (covers relative imports)."""
        if self._by_short is None:
            by_short: dict[str, list[str]] = {}
            for key in self._known:
                by_short.setdefault(key.rsplit(".", 1)[-1], []).append(key)
            self._by_short = by_short
        return self._by_short.get(name.rsplit(".", 1)[-1], [])


# ── Surface store ───────────────────────────────────────────────────────
@dataclass
class SurfaceView:
    """A class's member surface, flattened across resolved base classes."""

    members: set[str]
    methods: dict[str, MethodInfo]
    complete: bool


class SurfaceStore:
    """Lazily parses the files that define the classes under verification.

    The file being verified is parsed fresh on every call (an agent may have
    just written it), while every other file is parsed once and cached by
    (mtime, size).
    """

    def __init__(
        self,
        root: Path | None,
        index: ProjectIndex | None,
        local_classes: dict[str, ClassSurface] | None = None,
        local_functions: dict[str, MethodInfo] | None = None,
    ):
        self.root = root
        self.index = index
        self.local_classes = local_classes or {}
        self.local_functions = local_functions or {}
        self._imported: dict[Path, dict[str, str]] = {}
        self._resolvers: dict[Path, NameResolver] = {}

    def _facts_for(
        self, path: Path
    ) -> tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]]:
        """Return cached ``(classes, functions, imports)`` for *path*.

        The shared ``load_file_facts`` cache is keyed by (path, mtime, size),
        so a file is parsed once per edit no matter how many classes, bases,
        or verifications reference it.
        """
        module = module_name_for(path, self.root) if self.root else path.stem
        return load_file_facts(path, module, str(path))

    def _surfaces_for(self, path: Path) -> tuple[dict[str, ClassSurface], dict[str, MethodInfo]]:
        classes, functions, imported = self._facts_for(path)
        self._imported[path] = imported
        return classes, functions

    def imported_for(self, path: Path) -> dict[str, str]:
        """Imported-symbol map of *path*, from the shared parse cache."""
        cached = self._imported.get(path)
        if cached is not None:
            return cached
        _classes, _functions, imported = self._facts_for(path)
        self._imported[path] = imported
        return imported

    def resolver_for(self, path: Path, imported: dict[str, str]) -> NameResolver:
        cached = self._resolvers.get(path)
        if cached is not None and cached.imported == imported:
            return cached
        module = module_name_for(path, self.root) if self.root else path.stem
        known = set(self.index.symbols) if self.index else set()
        classes, functions = self._surfaces_for(path)
        known |= set(classes) | set(functions)
        resolver = NameResolver(module, imported, known)
        self._resolvers[path] = resolver
        return resolver

    def class_surface(self, qname: str) -> ClassSurface | None:
        if qname in self.local_classes:
            return self.local_classes[qname]
        if self.index is None or self.root is None:
            return None
        info = self.index.symbols.get(qname)
        if info is None or info.kind != "class":
            return None
        path = self.root / info.file
        classes, _functions = self._surfaces_for(path)
        return classes.get(qname)

    def function(self, qname: str) -> MethodInfo | None:
        if qname in self.local_functions:
            return self.local_functions[qname]
        if self.index is None or self.root is None:
            return None
        info = self.index.symbols.get(qname)
        if info is None or info.kind != "function":
            return None
        _classes, functions = self._surfaces_for(self.root / info.file)
        return functions.get(qname)

    def view(self, qname: str, _depth: int = 0) -> SurfaceView | None:
        """Flatten a class's member surface.

        Returns ``None`` when the surface is unknowable — the class is missing,
        defines ``__getattr__``, or a base could not be resolved deeply enough.
        """
        if _depth > _MAX_BASE_DEPTH:
            return None
        surface = self.class_surface(qname)
        if surface is None or surface.dynamic:
            return None

        members = set(surface.members)
        methods = dict(surface.methods)
        complete = True
        for base in surface.bases:
            if not base or base in EMPTY_BASES:
                continue
            resolver = self._resolver_for_definition(surface)
            base_qname = resolver.resolve_dotted(base) if resolver else None
            if base_qname is None or base_qname == qname:
                complete = False
                continue
            parent = self.view(base_qname, _depth + 1)
            if parent is None:
                complete = False
                continue
            members |= parent.members
            methods.update({k: v for k, v in parent.methods.items() if k not in methods})
            complete = complete and parent.complete
        return SurfaceView(members=members, methods=methods, complete=complete)

    def _resolver_for_definition(self, surface: ClassSurface) -> NameResolver | None:
        path = Path(surface.file)
        if not path.is_absolute() and self.root is not None:
            path = self.root / surface.file
        if not path.exists():
            return None
        # Imported symbols come from the shared parse cache — this used to
        # re-parse the whole file on every base-class lookup.
        return self.resolver_for(path, self.imported_for(path))


# ── Scope tracking ──────────────────────────────────────────────────────
def _bindings_of(node: ast.AST, *, is_function: bool) -> dict[str, str]:
    """Collect ``variable -> type name`` bindings introduced directly in *node*."""
    out: dict[str, str] = {}
    if is_function:
        for arg in _all_args(node):  # type: ignore[arg-type]
            if arg.annotation is not None:
                name = annotation_name(arg.annotation)
                if name:
                    out[arg.arg] = name
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name = annotation_name(child.annotation) if child.annotation else None
            if name is None and isinstance(child.value, ast.Call):
                name = call_name(child.value.func)
            if name:
                out[child.target.id] = name
        elif isinstance(child, ast.Assign) and isinstance(child.value, ast.Call):
            name = call_name(child.value.func)
            if name:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = name
    return out


def _all_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    a = node.args
    return [*getattr(a, "posonlyargs", []), *a.args, *a.kwonlyargs]


class _Scope:
    def __init__(self) -> None:
        self.stack: list[dict[str, str]] = [{}]
        self.class_qname: str | None = None
        self.class_stack: list[str | None] = [None]

    def push(self, bindings: dict[str, str], class_qname: str | None = None) -> None:
        self.stack.append(bindings)
        self.class_stack.append(class_qname)
        self.class_qname = class_qname

    def pop(self) -> None:
        self.stack.pop()
        self.class_stack.pop()
        self.class_qname = self.class_stack[-1]

    def lookup(self, name: str) -> str | None:
        for frame in reversed(self.stack):
            if name in frame:
                return frame[name]
        return None


# ── The verifier ────────────────────────────────────────────────────────
@dataclass
class _Verifier:
    path: Path
    root: Path | None
    index: ProjectIndex | None
    defined: set[str]
    dep_names: set[str]
    resolver: NameResolver
    store: SurfaceStore
    module: str
    dep_roots: set[str] | None = None
    dep_enumerated: set[str] | None = None
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0
    _dep_roots: set[str] = field(init=False)
    _dep_enumerated: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self._dep_roots = {n.split(".", 1)[0] for n in self.dep_names} | set(self.dep_roots or ())
        # Member checks need a module whose surface was actually enumerated;
        # a declared-but-uninstalled package knows nothing about its members,
        # so ``tomllib.loads`` on a declared-but-absent ``tomli`` must stay
        # silent rather than be reported as invented.
        if self.dep_enumerated is not None:
            self._dep_enumerated = set(self.dep_enumerated)
        else:
            self._dep_enumerated = {n for n in self.dep_names if "." not in n} | {
                r for r in self._dep_roots if r in self.dep_names
            }

    # -- helpers ---------------------------------------------------------
    def _finding(self, **kwargs: Any) -> None:
        self.findings.append(Finding(file=str(self.path), **kwargs))

    def _public(self, members: set[str]) -> list[str]:
        return sorted(m for m in members if not m.startswith("_"))

    def _display(self, qname: str) -> str:
        return qname.rsplit(".", 1)[-1]

    # -- entry point -----------------------------------------------------
    def run(self, tree: ast.AST) -> None:
        scope = _Scope()
        scope.stack = [_bindings_of(tree, is_function=False)]
        self._walk(tree, scope)

    def _walk(self, node: ast.AST, scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope.push(_bindings_of(child, is_function=True), scope.class_qname)
                self._walk(child, scope)
                scope.pop()
            elif isinstance(child, ast.ClassDef):
                qname = f"{self.module}.{child.name}" if self.module else child.name
                parent = scope.class_qname
                qname = f"{parent}.{child.name}" if parent else qname
                scope.push(_bindings_of(child, is_function=False), qname)
                self._walk(child, scope)
                scope.pop()
            else:
                if isinstance(child, ast.Call):
                    self._check_call(child, scope)
                self._walk(child, scope)

    # -- checks ----------------------------------------------------------
    def _resolve_self_chain(self, receiver: str, class_qname: str) -> str | None:
        """Resolve ``self.client`` / ``self`` to the type of the final attribute."""
        if receiver in ("self", "cls"):
            return class_qname
        parts = receiver.split(".")
        if parts[0] not in ("self", "cls"):
            return None
        qname = class_qname
        for attr in parts[1:]:
            surface = self.store.class_surface(qname)
            if surface is None:
                return None
            # Prefer instance-attribute type, then class attribute, then method return type.
            next_type = surface.instance_attrs.get(attr)
            if next_type is None and attr in surface.attributes:
                return None  # class attr without type info — cannot continue
            if next_type is None:
                return None
            qname = self.resolver.resolve_dotted(next_type)
            if qname is None:
                return None
        return qname

    def _check_call(self, call: ast.Call, scope: _Scope) -> None:
        name = call_name(call.func)
        if name is None:
            # Complex target ((a + b).foo(), super().x()) — cannot judge.
            return
        self.checked += 1
        receiver, _, attr = name.rpartition(".")
        if not receiver:
            self._check_bare_call(call, name)
            return
        if (
            receiver.startswith("self.")
            or receiver.startswith("cls.")
            or receiver in ("self", "cls")
        ):
            if scope.class_qname is None:
                return
            qname = self._resolve_self_chain(receiver, scope.class_qname)
            if qname is None:
                return
            self._check_member_call(call, qname, attr)
            return
        # A dependency module alias (`pd.read_excel`) is checked against the
        # loaded dependency API index.
        if self._check_dependency_call(call, receiver, attr):
            return
        type_name = scope.lookup(receiver)
        if not type_name:
            return
        qname = self.resolver.resolve_dotted(type_name)
        if qname is None:
            return
        self._check_member_call(call, qname, attr)

    def _check_bare_call(self, call: ast.Call, name: str) -> None:
        base = name.split(".")[0]
        if base not in self.defined and name not in self.defined:
            self._finding(
                line=call.lineno,
                kind=KIND_UNDEFINED_SYMBOL,
                symbol=name,
                message=f"`{name}` is not defined in this project or in any loaded dependency.",
                evidence={"kind": "call"},
                action=ACTION_INSPECT,
            )
            return
        # Grounded: check the call's arity when we can see the definition.
        qname = self.resolver.resolve(name)
        if qname is None:
            return
        info = self.store.function(qname)
        if info is None:
            surface = self.store.class_surface(qname)
            info = surface.methods.get("__init__") if surface else None
        if info is not None:
            self._check_arity(call, f"{self._display(qname)}()", info)

    def _check_dependency_call(self, call: ast.Call, receiver: str, attr: str) -> bool:
        """Verify ``alias.attr()`` against the dependency API index.

        Only modules whose member surface was actually enumerated are checked:
        for a declared-but-uninstalled package, or a submodule the loader never
        walked into, "not in the index" is not evidence of absence.
        """
        if not self._dep_roots:
            return False
        module = receiver if receiver in self._dep_roots else None
        if module is None:
            mapped = self.resolver.imported.get(receiver)
            if mapped and mapped.split(".")[0] in self._dep_roots:
                # Use the FULL mapped source, not its root: for
                # ``from numpy import random; random.seed()`` the check must
                # run against ``numpy.random.seed``, not ``numpy.seed``.
                # Collapsing to the root used to flag perfectly valid calls
                # on aliased submodules as invented APIs.
                module = mapped
        if module is None or module not in self._dep_enumerated:
            return False

        full = f"{module}.{attr}"
        if full in self.dep_names:
            return True

        members = [k.rsplit(".", 1)[-1] for k in self.dep_names if k.startswith(f"{module}.")]
        near = get_close_matches(attr, members, n=1, cutoff=_NEAR_MISS_CUTOFF)
        evidence: dict[str, Any] = {
            "available_methods": sorted(set(members))[:MAX_EVIDENCE_METHODS]
        }
        if near:
            evidence["did_you_mean"] = near[0]
        self._finding(
            line=call.lineno,
            kind=KIND_INVENTED_API,
            symbol=attr,
            owner=module,
            message=f"`{module}.{attr}()` does not exist.",
            evidence=evidence,
            confidence=CONFIDENCE_DETERMINISTIC,
        )
        return True

    def _check_member_call(self, call: ast.Call, qname: str, attr: str) -> None:
        view = self.store.view(qname)
        if view is None:
            return  # Unknown surface — stay silent rather than guess.
        owner = self._display(qname)

        if attr in view.members:
            info = view.methods.get(attr)
            if info is not None:
                self._check_arity(call, f"{owner}.{attr}()", info)
            return

        if not view.complete:
            # An unresolved base (a stdlib ABC, a third-party parent) may
            # legitimately provide this member, so absence cannot be proven.
            # Reporting anyway used to produce false "high confidence"
            # findings on real code — e.g. ``CaseInsensitiveDict.items()``,
            # supplied by the unresolvable ``MutableMapping`` base, was
            # reported with ``did_you_mean: lower_items``.
            return

        public = self._public(view.members)
        evidence: dict[str, Any] = {"available_methods": public[:MAX_EVIDENCE_METHODS]}
        near = get_close_matches(attr, public, n=1, cutoff=_NEAR_MISS_CUTOFF)
        if near:
            evidence["did_you_mean"] = near[0]
        self._finding(
            line=call.lineno,
            kind=KIND_INVENTED_API,
            symbol=attr,
            owner=owner,
            message=f"`{owner}.{attr}()` does not exist.",
            evidence=evidence,
            confidence=CONFIDENCE_DETERMINISTIC,
        )

    def _check_arity(self, call: ast.Call, label: str, info: MethodInfo) -> None:
        if info.is_property:
            return  # Properties are accessed, not called.
        if not info.arity_trustworthy:
            return  # A decorator may have rewritten the signature.
        if any(isinstance(a, ast.Starred) for a in call.args):
            return  # Caller unpacks a sequence — count is unknown.

        positional = len(call.args)
        keyword_names = [kw.arg for kw in call.keywords if kw.arg is not None]
        keyword_set = set(keyword_names)
        has_starstar = any(kw.arg is None for kw in call.keywords)

        # Too many positional arguments is an error no matter how the call
        # spells its keywords.
        if info.max_args is not None and positional > info.max_args:
            self._arity_finding(call, label, info, positional, keyword_names, "too many arguments")
            return

        # Too few: required parameters may also be satisfied by keyword —
        # ``f(a, b)`` called as ``f(a=1, b=2)`` is valid, and keyword-call
        # style is the norm in modern Python (``verify_tree(tree, path=...)``).
        if not has_starstar:
            unfilled = info.required_positional[positional:]
            missing = [n for n in unfilled if n not in keyword_set]
            missing += [n for n in sorted(info.required_keywords) if n not in keyword_set]
            # **kwargs on the definition cannot fill *named* required
            # parameters, so a missing name is still conclusive.
            if missing:
                self._arity_finding(
                    call, label, info, positional, keyword_names, "too few arguments"
                )
                return

        if keyword_names and not info.has_kwargs and not has_starstar:
            unknown = [k for k in keyword_names if k not in info.keywords]
            if unknown:
                self._finding(
                    line=call.lineno,
                    kind=KIND_WRONG_CALL,
                    symbol=label,
                    message=f"`{label}` does not accept keyword argument(s): "
                    f"{', '.join(sorted(unknown))}.",
                    evidence={
                        "expected": ", ".join(sorted(info.keywords)) or "none",
                        "actual": ", ".join(sorted(unknown)),
                    },
                )

    def _arity_finding(
        self,
        call: ast.Call,
        label: str,
        info: MethodInfo,
        positional: int,
        keyword_names: list[str],
        problem: str,
    ) -> None:
        if keyword_names:
            actual = f"{positional} positional + {len(keyword_names)} keyword argument(s)"
        else:
            actual = f"{positional} argument(s)"
        self._finding(
            line=call.lineno,
            kind=KIND_WRONG_CALL,
            symbol=label,
            message=f"`{label}` called with {problem}: expected {info.describe()}, got {actual}.",
            evidence={"expected": info.describe(), "actual": actual},
        )


def verify_tree(
    tree: ast.AST,
    *,
    path: Path,
    root: Path | None,
    index: ProjectIndex | None,
    defined: set[str],
    dep_names: set[str],
    imported_symbols: dict[str, str],
    dep_roots: set[str] | None = None,
    dep_enumerated: set[str] | None = None,
) -> tuple[list[Finding], int]:
    """Verify every call site in *tree* and return ``(findings, checked)``.

    *dep_roots* optionally supplies trusted dependency root names beyond those
    derivable from *dep_names* (see :func:`valid_imported_names`);
    *dep_enumerated* lists the dependency modules whose member surface is
    known, which is what makes invented-API reporting sound.
    """
    module = module_name_for(path, root) if root else path.stem
    local_classes, local_functions = extract_file_surfaces(
        tree, module=module, file_label=str(path)
    )
    known = set(index.symbols) if index else set()
    known |= set(local_classes) | set(local_functions)
    resolver = NameResolver(module, imported_symbols, known)
    store = SurfaceStore(root, index, local_classes, local_functions)

    verifier = _Verifier(
        path=path,
        root=root,
        index=index,
        defined=defined,
        dep_names=dep_names,
        dep_roots=dep_roots,
        dep_enumerated=dep_enumerated,
        resolver=resolver,
        store=store,
        module=module,
    )
    verifier.run(tree)
    return verifier.findings, verifier.checked


def verify_file_source(
    source: str,
    *,
    path: Path,
    root: Path | None,
    index: ProjectIndex | None,
    defined: set[str],
    dep_names: set[str],
) -> tuple[list[Finding], int]:
    """Parse *source* and verify every call site in it."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        finding = Finding(
            file=str(path),
            line=exc.lineno or 1,
            kind=KIND_SYNTAX_ERROR,
            symbol="<syntax>",
            message=f"SyntaxError: {exc.msg}",
            evidence={"offset": exc.offset or 0},
            action=ACTION_INSPECT,
        )
        return [finding], 0
    try:
        imported = StructureExtractor().parse_source(source).imported_symbols or {}
    except Exception:  # noqa: BLE001 - defensive
        imported = {}
    return verify_tree(
        tree,
        path=path,
        root=root,
        index=index,
        defined=defined,
        dep_names=dep_names,
        imported_symbols=imported,
    )
