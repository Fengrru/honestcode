"""Shared AST utilities for HonestCode."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "call_base",
    "call_name",
    "importable_root",
    "module_name_for",
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
