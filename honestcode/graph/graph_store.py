"""Persistent call-graph and full-text search index backed by SQLite.

``GraphCache`` persists the project-wide call graph (definitions + references)
to a SQLite database keyed by a stable hash of the project root. Freshness is
decided by comparing stored SHA-256 hashes of every ``.py`` file against the
current tree, so graph queries (``explore_call_graph``, ``find_dead_code``,
``impact``, ``affected``) read from disk instead of re-parsing the project on
every call.

The same database hosts a FTS5 virtual table (trigram tokenizer) over file
contents. ``search_code`` uses it to shrink a regex scan to a *superset* of
candidate files: it extracts a literal substring the pattern requires and
matches it as a substring, so even matches inside longer tokens are found —
a plain token-prefix index would have missed those. When no required literal
can be derived (or FTS5 is unavailable), the caller falls back to a full scan,
keeping results identical to an unindexed search in all cases.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["CallGraph", "GraphCache"]

_DB_VERSION = 1


@dataclass
class CallGraph:
    """Project-wide definition/reference graph.

    ``definitions`` maps a symbol name to ``(file, line, end_line, kind)``.
    ``references``  maps a symbol name to ``(file, line, kind, context)`` where
    ``context`` is the callee when the reference is a call site (else ``None``).
    """

    definitions: dict[str, list[tuple[Path, int, int, str]]] = field(default_factory=dict)
    references: dict[str, list[tuple[Path, int, str, str | None]]] = field(default_factory=dict)


def _python_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if not p.name.startswith(".") and "__pycache__" not in p.parts
    )


def _file_hashes(root: Path) -> dict[str, str]:
    """Map each relative .py path to a SHA-256 of its bytes."""
    hashes: dict[str, str] = {}
    for p in _python_files(root):
        try:
            hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class GraphCache:
    """SQLite-backed cache of a project's call graph + FTS5 file index."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def for_root(cls, root: Path, cache_dir: Path) -> GraphCache:
        resolved = root.resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cls(cache_dir / f"{resolved.name}_{_stable_hash(str(resolved))}.graph.db")

    # -- freshness ---------------------------------------------------------

    def is_fresh(self, root: Path) -> bool:
        """True when the cached graph covers exactly the current file tree."""
        if not self.path.exists():
            return False
        conn = None
        try:
            conn = sqlite3.connect(str(self.path))
            cur = conn.cursor()
            row = cur.execute("SELECT value FROM meta WHERE key='hashes'").fetchone()
            root_row = cur.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                conn.close()
        if row is None or root_row is None:
            return False
        if root_row[0] != str(root.resolve()):
            return False
        try:
            stored = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return False
        return stored == _file_hashes(root)

    # -- persistence -------------------------------------------------------

    def save(self, graph: CallGraph, root: Path) -> None:
        """Persist the graph plus a FTS5 index of the current file tree."""
        root = root.resolve()
        hashes = _file_hashes(root)
        files = sorted(hashes.keys())
        conn = sqlite3.connect(str(self.path))
        try:
            conn.execute("DROP TABLE IF EXISTS definitions")
            conn.execute("DROP TABLE IF EXISTS refs")
            conn.execute("DROP TABLE IF EXISTS meta")
            conn.execute(
                "CREATE TABLE definitions ("
                "symbol TEXT NOT NULL, file TEXT NOT NULL, "
                "line INTEGER NOT NULL, end_line INTEGER NOT NULL, kind TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE refs ("
                "caller TEXT NOT NULL, file TEXT NOT NULL, "
                "line INTEGER NOT NULL, kind TEXT NOT NULL, context TEXT)"
            )
            conn.executemany(
                "INSERT INTO definitions VALUES (?, ?, ?, ?, ?)",
                [
                    (name, str(p), line, end_line, kind)
                    for name, locs in graph.definitions.items()
                    for p, line, end_line, kind in locs
                ],
            )
            conn.executemany(
                "INSERT INTO refs VALUES (?, ?, ?, ?, ?)",
                [
                    (name, str(p), line, kind, context)
                    for name, locs in graph.references.items()
                    for p, line, kind, context in locs
                ],
            )
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [
                    ("version", str(_DB_VERSION)),
                    ("root", str(root)),
                    ("files", json.dumps(files)),
                    ("hashes", json.dumps(hashes)),
                ],
            )
            conn.commit()
            self._rebuild_fts(conn, root, files)
        finally:
            conn.close()

    def load(self) -> CallGraph | None:
        """Load the graph from disk, or ``None`` if missing/corrupt."""
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(str(self.path))
            cur = conn.cursor()
            definitions: dict[str, list[tuple[Path, int, int, str]]] = {}
            for name, f, line, end_line, kind in cur.execute(
                "SELECT symbol, file, line, end_line, kind FROM definitions"
            ):
                definitions.setdefault(name, []).append((Path(f), line, end_line, kind))
            references: dict[str, list[tuple[Path, int, str, str | None]]] = {}
            for name, f, line, kind, context in cur.execute(
                "SELECT caller, file, line, kind, context FROM refs"
            ):
                references.setdefault(name, []).append((Path(f), line, kind, context))
            conn.close()
            return CallGraph(definitions=definitions, references=references)
        except sqlite3.Error as exc:
            logger.warning("Failed to load graph cache %s: %s", self.path, exc)
            return None

    # -- FTS5 full-text search --------------------------------------------

    def _rebuild_fts(self, conn: sqlite3.Connection, root: Path, files: list[str]) -> None:
        try:
            conn.execute("DROP TABLE IF EXISTS fts_files")
            conn.execute(
                "CREATE VIRTUAL TABLE fts_files USING fts5("
                "path UNINDEXED, content, tokenize='trigram')"
            )
        except sqlite3.Error:
            logger.debug("FTS5 unavailable; search_code will fall back to regex scans")
            return
        for rel in files:
            path = root / rel
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            conn.execute("INSERT INTO fts_files (path, content) VALUES (?, ?)", (rel, content))
        conn.commit()

    @staticmethod
    def _required_literal(pattern: str) -> str | None:
        """Return a literal substring every match of *pattern* must contain.

        Conservative by design: a maximal ``[A-Za-z0-9_]+`` run counts only
        when both neighbours are plain literal characters (alphanumeric,
        underscore, space, or a string boundary). Anything else — a quantifier
        after the run (``foo*`` can match ``fo``), an alternation, an escaped
        metachar before it — disqualifies the run, because guessing wrong here
        would silently drop real matches.

        Returns ``None`` when no run of >= 3 characters qualifies (the trigram
        index cannot help below three characters).
        """
        literal_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ ")
        best = ""
        n = len(pattern)
        i = 0
        while i < n:
            if pattern[i] not in literal_chars or pattern[i] == " ":
                i += 1
                continue
            j = i
            while j < n and pattern[j] in literal_chars and pattern[j] != " ":
                j += 1
            before_ok = i == 0 or pattern[i - 1] in (" ",)
            after_ok = j == n or pattern[j] in (" ",)
            if before_ok and after_ok and (j - i) > len(best):
                best = pattern[i:j]
            i = j
        return best if len(best) >= 3 else None

    def search_candidate_files(self, pattern: str) -> list[str] | None:
        """Return files that *may* match ``pattern`` using FTS5 substring search.

        Uses the trigram index to find files containing the literal substring
        that :meth:`_required_literal` proved the pattern requires. Because the
        match is substring-based (not token-prefix), occurrences inside longer
        tokens are still found and the candidate set is a true superset; when
        no required literal can be derived, ``None`` tells the caller to fall
        back to a full scan. Returns an empty list when the required literal
        occurs nowhere (short-circuit to no results).
        """
        literal = self._required_literal(pattern)
        if literal is None:
            return None
        try:
            conn = sqlite3.connect(str(self.path))
            try:
                cur = conn.cursor()
                paths: list[str] = []
                for (path,) in cur.execute(
                    "SELECT path FROM fts_files WHERE fts_files MATCH ?",
                    (f'"{literal}"',),
                ):
                    paths.append(path)
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return sorted(set(paths))
