"""Shared AST utilities for HonestCode."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["call_name", "call_base", "module_name_for"]


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
