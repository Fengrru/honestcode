"""HonestCode: verify AI-generated code against project structure and installed APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from honestcode import honest, mcp, sandbox, structure

__version__ = "0.4.0"
__all__ = ["honest", "mcp", "sandbox", "structure"]


def __getattr__(name: str):
    if name in __all__:
        import importlib

        return importlib.import_module(f"honestcode.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
