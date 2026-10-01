"""
Project-wide symbol index.

Builds a unified view of every function, class, and variable defined in a
project, and caches the result to disk.

Invalidation is metadata-driven: the cache records each file's
``(mtime_ns, size)`` and re-parses only files whose metadata changed, so an
agent that just edited one file gets a fresh index without a full re-parse.
This is what makes auto-indexing on every ``scan_file`` affordable — and it
means a long-lived MCP server session never verifies against a stale index
(the previous design cached forever and produced false "undefined symbol"
reports for symbols the agent had just written).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from honestcode.structure.extractor import StructureExtractor
from honestcode.structure.utils import module_name_for

logger = logging.getLogger(__name__)

__all__ = ["SymbolInfo", "SymbolIndex", "ProjectIndex", "get_project_index"]

# Bumped whenever the cache payload gains a field the verifier relies on
# (v2 added file_names/module_symbols for re-export resolution); older cache
# files are ignored so a stale on-disk index cannot silently degrade results.
_INDEX_CACHE_VERSION = 2


@dataclass
class SymbolInfo:
    file: str
    line: int
    kind: str
    exported: bool = True


@dataclass
class SymbolIndex:
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)

    @property
    def exported_symbols(self) -> dict[str, SymbolInfo]:
        return {n: i for n, i in self.symbols.items() if i.exported}

    @property
    def local_symbols(self) -> dict[str, SymbolInfo]:
        return {n: i for n, i in self.symbols.items() if not i.exported}


@dataclass
class ProjectIndex:
    root: str
    symbols: dict[str, SymbolInfo]
    files: list[str]
    cache_key: str = ""
    cached: bool = False
    # rel path (posix) -> [mtime_ns, size]; used for cheap freshness checks.
    stats: dict[str, list[int]] = field(default_factory=dict)
    # rel path (posix) -> {qualified name -> SymbolInfo}; enables incremental
    # rebuilds: unchanged files reuse their per-file symbols without parsing.
    file_symbols: dict[str, dict[str, SymbolInfo]] = field(default_factory=dict)
    # rel path (posix) -> names bound at that module's top level (definitions
    # plus imported aliases). Makes re-exports resolvable: in
    # ``from .compat import urlparse`` the name ``urlparse`` is not a symbol
    # of ``requests.compat`` (it is itself an import there) but it is bound in
    # that module, so the import is grounded.
    file_names: dict[str, set[str]] = field(default_factory=dict)
    # module qname -> module-level bound names (aggregated from file_names).
    module_symbols: dict[str, set[str]] = field(default_factory=dict)

    def to_sqlite(self, db_path: str | Path) -> None:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS symbols")
        conn.execute(
            "CREATE TABLE symbols (name TEXT PRIMARY KEY, file TEXT, line INTEGER, kind TEXT, exported INTEGER)"  # noqa: E501
        )
        for name, info in self.symbols.items():
            conn.execute(
                "INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?, ?)",
                (name, info.file, info.line, info.kind, int(info.exported)),
            )
        conn.execute("DROP TABLE IF EXISTS meta")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES (?, ?)", ("root", self.root))
        conn.execute("INSERT INTO meta VALUES (?, ?)", ("files", json.dumps(self.files)))
        conn.commit()
        conn.close()

    @classmethod
    def from_sqlite(cls, db_path: str | Path) -> ProjectIndex | None:
        import sqlite3

        p = Path(db_path)
        if not p.exists():
            return None
        try:
            conn = sqlite3.connect(str(p))
            cur = conn.cursor()
            cur.execute("SELECT name, file, line, kind, exported FROM symbols")
            symbols = {
                name: SymbolInfo(file=f, line=line, kind=kind, exported=bool(exported))
                for name, f, line, kind, exported in cur.fetchall()
            }
            cur.execute("SELECT key, value FROM meta")
            meta = dict(cur.fetchall())
            conn.close()
            return cls(
                root=meta.get("root", ""),
                symbols=symbols,
                files=json.loads(meta.get("files", "[]")),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load SQLite index from %s: %s", db_path, e)
            return None


def _iter_py_files(root: Path) -> dict[str, Path]:
    """Map every indexable .py file (posix rel path) to its absolute path."""
    files: dict[str, Path] = {}
    for p in sorted(root.rglob("*.py")):
        if p.name.startswith(".") or "__pycache__" in p.parts:
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        files[rel] = p
    return files


def _stat_of(p: Path) -> list[int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _tree_stats(root: Path) -> dict[str, list[int]]:
    """Current ``(mtime_ns, size)`` for every indexable file, keyed by rel path."""
    out: dict[str, list[int]] = {}
    for rel, p in _iter_py_files(root).items():
        st = _stat_of(p)
        if st is not None:
            out[rel] = st
    return out


def _stats_fresh(root: Path, stats: dict[str, list[int]]) -> bool:
    """True when no indexed file was added, removed, or edited since *stats*.

    Compares ``(mtime_ns, size)`` only — no file contents are read. A file
    rewritten twice within the same mtime tick *and* at the same size is the
    one case this misses; the previous design (hashing every file on every
    check) caught it but made each scan pay a full-tree read.
    """
    current = _tree_stats(root)
    return current == stats


def _index_cache_key(stats: dict[str, list[int]]) -> str:
    """Deterministic key over the tree's file list and metadata (no file reads)."""
    h = hashlib.sha256()
    for rel in sorted(stats):
        h.update(f"{rel}:{stats[rel][0]}:{stats[rel][1]}".encode())
    return h.hexdigest()


@dataclass
class ProjectIndexJSON:
    """Serialization helpers for the on-disk JSON cache."""

    @staticmethod
    def dump(idx: ProjectIndex) -> dict:
        return {
            "version": _INDEX_CACHE_VERSION,
            "root": idx.root,
            "symbols": {n: asdict(i) for n, i in idx.symbols.items()},
            "files": idx.files,
            "cache_key": idx.cache_key,
            "stats": idx.stats,
            "file_symbols": {
                rel: {n: asdict(i) for n, i in syms.items()}
                for rel, syms in idx.file_symbols.items()
            },
            "file_names": {rel: sorted(names) for rel, names in idx.file_names.items()},
        }

    @staticmethod
    def load(data: dict) -> ProjectIndex:
        symbols = {n: SymbolInfo(**i) for n, i in data.get("symbols", {}).items()}
        file_symbols = {
            rel: {n: SymbolInfo(**i) for n, i in syms.items()}
            for rel, syms in data.get("file_symbols", {}).items()
        }
        root = data.get("root", "")
        file_names = {rel: set(names) for rel, names in data.get("file_names", {}).items()}
        return ProjectIndex(
            root=root,
            symbols=symbols,
            files=data.get("files", []),
            cache_key=data.get("cache_key", ""),
            stats={k: list(v) for k, v in data.get("stats", {}).items()},
            file_symbols=file_symbols,
            file_names=file_names,
            module_symbols=_build_module_symbols(root, file_names),
        )


def _module_name(file: Path, root: Path) -> str:
    """Infer a dotted module name from *file* relative to *root*.

    Kept as a thin alias; the single implementation lives in
    :func:`honestcode.structure.utils.module_name_for`.
    """
    return module_name_for(file, root)


def _merge(rel: str, module: str, res) -> tuple[dict[str, SymbolInfo], set[str]]:
    """Turn a ParseResult into ``({qualified name: SymbolInfo}, bound names)``.

    The second element lists every name bound at the module's top level —
    definitions plus imported aliases — which is what makes re-exports
    (``from .compat import urlparse``) resolvable against the index.
    """
    symbols: dict[str, SymbolInfo] = {}
    names: set[str] = set()

    def _add(name: str, line: int, kind: str) -> None:
        full_name = f"{module}.{name}" if module and name else (name or module)
        exported = not name.split(".")[-1].startswith("_") if name else True
        symbols[full_name] = SymbolInfo(file=rel, line=line, kind=kind, exported=exported)
        if "." not in name and name:
            names.add(name)

    for fn, (s, _e) in res.func_defs.items():
        _add(fn, s + 1, "function")
    for cn, (s, _e) in res.class_defs.items():
        _add(cn, s + 1, "class")
    for vn, (s, _e) in res.var_defs.items():
        _add(vn, s + 1, "variable")
    names.update((res.imported_symbols or {}).keys())

    return symbols, names


def _index_directory(
    root: Path,
    extractor: StructureExtractor,
    previous: ProjectIndex | None = None,
) -> ProjectIndex:
    """Build the index, reusing per-file symbols for files whose metadata is
    unchanged since *previous* (incremental rebuild)."""
    files_map = _iter_py_files(root)
    stats = {rel: st for rel in files_map if (st := _stat_of(files_map[rel])) is not None}
    prev_stats = previous.stats if previous else {}
    prev_syms = previous.file_symbols if previous else {}
    prev_names = previous.file_names if previous else {}

    symbols: dict[str, SymbolInfo] = {}
    file_symbols: dict[str, dict[str, SymbolInfo]] = {}
    file_names: dict[str, set[str]] = {}
    files: list[str] = []
    reused = 0
    for rel in sorted(files_map):
        files.append(rel)
        unchanged = prev_stats.get(rel) == stats.get(rel)
        if unchanged and rel in prev_syms and rel in prev_names:
            file_symbols[rel] = prev_syms[rel]
            file_names[rel] = prev_names[rel]
            reused += 1
        else:
            module = module_name_for(files_map[rel], root)
            per_file, names = _merge(rel, module, extractor.parse_file(files_map[rel]))
            file_symbols[rel] = per_file
            file_names[rel] = names
        symbols.update(file_symbols[rel])

    if previous is not None and reused:
        logger.info(
            "Incremental index for %s: %d file(s) re-parsed, %d reused",
            root,
            len(files) - reused,
            reused,
        )

    return ProjectIndex(
        root=str(root),
        symbols=symbols,
        files=files,
        cache_key=_index_cache_key(stats),
        stats=stats,
        file_symbols=file_symbols,
        file_names=file_names,
        module_symbols=_build_module_symbols(str(root), file_names),
    )


def _build_module_symbols(root: str, file_names: dict[str, set[str]]) -> dict[str, set[str]]:
    """Aggregate per-file bound names into ``module qname -> names``."""
    root_path = Path(root) if root else None
    out: dict[str, set[str]] = {}
    for rel, names in file_names.items():
        if not names:
            continue
        module = module_name_for(root_path / rel, root_path) if root_path else ""
        if not module:
            continue
        out.setdefault(module, set()).update(names)
    return out


# Module-level cache to avoid re-indexing within a single process. Entries are
# validated against the tree's metadata on every access (see get_project_index),
# so an edited project is rebuilt instead of served stale.
_index_cache: dict[str, ProjectIndex] = {}


def get_project_index(
    root: str | Path,
    force_rebuild: bool = False,
    cache_dir: str | Path | None = None,
) -> ProjectIndex:
    """Return (and cache) the project index for *root*.

    ``ProjectIndex.cached`` reports whether the result was served unchanged
    from the in-memory or on-disk cache (``False`` when it was (re)built).

    The in-memory entry is validated against the tree's ``(mtime_ns, size)``
    metadata on every call, so callers never receive an index that predates an
    edit. The on-disk cache is used the same way.
    """
    root = Path(root)
    resolved = root.resolve()
    cache_dir = Path(
        cache_dir
        or os.environ.get("HONESTCODE_INDEX_DIR")
        or os.path.expanduser("~/.cache/honestcode")
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{resolved.name}_{_stable_hash(str(resolved))}.json"

    previous: ProjectIndex | None = None
    if not force_rebuild:
        cached = _index_cache.get(str(resolved))
        if cached is not None:
            if _stats_fresh(resolved, cached.stats):
                # Mark the shared instance so callers can tell that this
                # response was served unchanged (a rebuild creates a fresh
                # object with cached=False).
                cached.cached = True
                return cached
            previous = cached  # stale: fall through to an incremental rebuild
            _index_cache.pop(str(resolved), None)

        if previous is None and cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if data.get("stats") is not None and data.get("version") == _INDEX_CACHE_VERSION:
                    disk_idx = ProjectIndexJSON.load(data)
                    if _stats_fresh(resolved, disk_idx.stats):
                        disk_idx.cached = True
                        _index_cache[str(resolved)] = disk_idx
                        return disk_idx
                    previous = disk_idx  # reuse its per-file symbols
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to load cache %s: %s", cache_path, e)

    extractor = StructureExtractor()
    idx = _index_directory(resolved, extractor, previous=previous)
    idx.cached = False

    try:
        cache_path.write_text(
            json.dumps(ProjectIndexJSON.dump(idx), indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to write cache %s: %s", cache_path, e)

    _index_cache[str(resolved)] = idx
    return idx


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for p in sorted(root.rglob("*.py")):
        if p.name.startswith(".") or "__pycache__" in p.parts:
            continue
        try:
            hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            continue
    return hashes
