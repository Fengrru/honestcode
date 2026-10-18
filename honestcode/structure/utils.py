"""Shared AST utilities for HonestCode."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
import time
import tokenize
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "call_base",
    "call_name",
    "importable_root",
    "iter_python_files",
    "module_name_for",
    "prune_cache_files",
    "read_python_source",
    "stdlib_resolves",
    "stdlib_roots",
]


@lru_cache(maxsize=1)
def stdlib_roots() -> frozenset[str]:
    """Top-level module names shipped with the interpreter.

    Used to trust imports from the standard library: ``from typing import
    TypeVar`` cannot be an invented symbol, but without this the verifier
    rejected every stdlib import whose package was not in the dependency list
    and reported real code as hallucinated.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    return frozenset(sys.builtin_module_names)


@lru_cache(maxsize=4096)
def stdlib_resolves(dotted: str) -> bool | None:
    """Whether *dotted* (e.g. ``typing.TypeVar``) resolves inside the stdlib.

    Returns ``True`` when the attribute exists, ``False`` when the module can
    be imported but demonstrably lacks it (an invented stdlib symbol), and
    ``None`` when the question cannot be answered on this platform — callers
    must then stay silent rather than guess. The standard library is real,
    importable code, so unlike third-party packages it can be checked exactly;
    only stdlib-only names are ever imported here.
    """
    parts = dotted.split(".")
    if not parts or not parts[0] or parts[0] not in stdlib_roots():
        return None

    obj = None
    consumed = 0
    # Import the longest importable module prefix: ``urllib.parse.urlparse``
    # needs ``import urllib.parse`` — a getattr walk from ``urllib`` alone
    # would miss the submodule.
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        try:
            obj = importlib.import_module(prefix)
            consumed = i
            break
        except ImportError:
            continue
        except Exception:  # noqa: BLE001 - broken stdlib module, cannot judge
            return None
    if obj is None:
        return None

    for part in parts[consumed:]:
        try:
            obj = getattr(obj, part)
        except AttributeError:
            return False
        except Exception:  # noqa: BLE001 - property/module hook misbehaving
            return None
    return True


@lru_cache(maxsize=2048)
def importable_root(name: str) -> bool:
    """Whether the top-level module *name* is importable in this interpreter.

    Used to recognise an undeclared third-party import root as real code: the
    package exists in the environment even though no manifest declares it
    (``from requests import get`` in a project whose ``requirements.txt`` only
    lists pyyaml is an undeclared dependency, not a hallucination). Only the
    top-level name is ever probed, and ``find_spec`` does not execute the
    module — parent packages are not imported either.
    """
    if not name or not name.isidentifier():
        return False
    if name in stdlib_roots():
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError, TypeError, OSError):
        return False


# Directories that never contain project source worth indexing. Hidden
# directories (``.venv``, ``.git``, ``.tox``, ...) are skipped by prefix, and the
# named ones below cover the common non-hidden artifact trees. Without this,
# following the README's own development setup (``python -m venv .venv``) made
# every scan index the entire virtual environment.
_SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "site-packages",
        "dist-packages",
        "venv",
        "build",
        "dist",
        "htmlcov",
    }
)


def _is_skipped_part(part: str) -> bool:
    return part.startswith(".") or part in _SKIP_DIR_NAMES or part.endswith(".egg-info")


def iter_python_files(root: Path | str) -> Iterator[Path]:
    """Yield every indexable ``.py`` file under *root*, sorted by path.

    Skips hidden directories (which covers ``.venv``/``.git``/``.tox`` and the
    other tool caches), virtual environments and build artifacts. This is the
    single traversal used by the symbol index, the call graph, the watcher and
    the MCP tools so their notions of "the project's files" cannot drift apart.
    """
    root = Path(root)
    for path in sorted(root.rglob("*.py")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(_is_skipped_part(part) for part in rel.parts[:-1]):
            continue
        if path.name.startswith(".") or path.name in _SKIP_DIR_NAMES:
            continue
        yield path


def read_python_source(path: Path | str) -> str | None:
    """Read Python source honouring its PEP 263 coding declaration.

    ``Path.read_text(encoding="utf-8")`` raises on any legally non-UTF-8 source
    file, and a single such file used to abort an entire project index build.
    ``tokenize.open`` decodes exactly what the interpreter itself would, and a
    genuinely unreadable file returns ``None`` so callers can skip it instead of
    failing the scan.
    """
    try:
        with tokenize.open(str(path)) as handle:
            return handle.read()
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError):
        return None


def prune_cache_files(
    directory: Path | str, patterns: tuple[str, ...], max_age_days: int = 30
) -> None:
    """Delete cache files matching *patterns* that have not been touched recently.

    Symbol/graph caches are stored one file per project root and are entirely
    regenerable. Without a bound they accumulate one entry per temporary or
    abandoned project forever. Only genuinely old files are removed, so an
    active project's cache is never collected; a deletion that loses a race
    with another process is ignored.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return
    cutoff = time.time() - max_age_days * 86400
    for pattern in patterns:
        for path in directory.glob(pattern):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def call_name(node: ast.expr) -> str | None:
    """Return a dotted name for a call target (e.g. ``obj.method``).

    Returns ``None`` for complex expressions (e.g. ``(a + b).foo()``).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
    return None


def call_base(name: str) -> str:
    """Return the first component of a dotted name."""
    return name.split(".")[0]


def module_name_for(path: Path, root: Path) -> str:
    """Infer a dotted module name for *path* relative to *root*.

    Only strips the ``src`` prefix when it is a standalone directory component
    (i.e. the standard ``src`` layout) and there are further path segments.
    """
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path
    parts = list(rel.with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)
