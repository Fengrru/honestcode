"""
HonestRouter: route natural-language queries to the right tool and detect
likely hallucinations by checking project symbols and installed dependency APIs.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from honestcode.structure.utils import module_name_for

if TYPE_CHECKING:
    from honestcode.mcp.knowledge_base import APIKnowledgeBase

__all__ = ["ToolIntent", "RouteChoice", "HonestRouter"]


class ToolIntent(Enum):
    INDEX = "index"
    CHECK_SYMBOL = "check_symbol"
    CHECK_API = "check_api"
    EXECUTE_CODE = "execute_code"
    SCAN_FILE = "scan_file"
    FIND_DEAD_CODE = "find_dead_code"
    EXPLORE_CALL_GRAPH = "explore_call_graph"
    SEARCH_CODE = "search_code"
    VALIDATE_TYPES = "validate_types"
    UNKNOWN = "unknown"


@dataclass
class RouteChoice:
    intent: ToolIntent
    confidence: float
    reason: str


class HonestRouter:
    """Route user queries and evaluate code suggestions against the index."""

    def __init__(
        self,
        project_symbols: dict[str, Any],
        dep_symbols: set[str],
        dep_kb: APIKnowledgeBase | None = None,
        project_root: str | Path | None = None,
    ):
        self.project_symbols = project_symbols
        self.dep_symbols = dep_symbols
        self.dep_kb = dep_kb
        self.project_root = Path(project_root) if project_root else None

    @staticmethod
    def choose_tool(query: str) -> RouteChoice:
        q = query.lower()
        patterns: list[tuple[list[str], ToolIntent, str]] = [
            (
                ["index", "scan project", "build index"],
                ToolIntent.INDEX,
                "query asks to index project",
            ),
            (
                ["symbol", "defined", "function exists"],
                ToolIntent.CHECK_SYMBOL,
                "query checks a symbol",
            ),
            (
                ["api", "library", "pandas", "numpy"],
                ToolIntent.CHECK_API,
                "query checks an external API",
            ),
            (
                ["run", "execute", "test code"],
                ToolIntent.EXECUTE_CODE,
                "query asks to execute code",
            ),
            (["scan file", "check file"], ToolIntent.SCAN_FILE, "query asks to scan a file"),
            (["dead code", "unused"], ToolIntent.FIND_DEAD_CODE, "query asks for dead code"),
            (
                ["call graph", "caller", "callee"],
                ToolIntent.EXPLORE_CALL_GRAPH,
                "query explores call graph",
            ),
            (["search", "find code"], ToolIntent.SEARCH_CODE, "query searches code"),
            (["type", "validate"], ToolIntent.VALIDATE_TYPES, "query validates types"),
        ]
        for keywords, intent, reason in patterns:
            if any(k in q for k in keywords):
                return RouteChoice(intent=intent, confidence=0.7, reason=reason)
        return RouteChoice(intent=ToolIntent.UNKNOWN, confidence=0.0, reason="no clear intent")

    def route(self, query: str, file_path: str | None = None) -> dict:
        """Route a query and return a confidence judgement."""
        choice = self.choose_tool(query)

        if choice.intent == ToolIntent.CHECK_API:
            # Find dotted API references like pandas.read_csv
            matches = re.findall(r"\b([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+)\b", query)
            for api in matches:
                return self._check_api(api)
            return {"intent": "check_api", "result": "no API reference found"}

        if choice.intent == ToolIntent.CHECK_SYMBOL:
            words = re.findall(r"\b([a-zA-Z_]\w{2,})\b", query)
            candidates = [w for w in words if w[0].islower()]
            if not candidates:
                return {"intent": "check_symbol", "result": "no symbol candidates"}
            symbol = candidates[0]
            defined = symbol in self.project_symbols
            return {
                "intent": "check_symbol",
                "symbol": symbol,
                "defined": defined,
                "confidence": 0.8 if defined else 0.3,
            }

        if choice.intent == ToolIntent.SCAN_FILE and file_path:
            return {"intent": "scan_file", "file": file_path}

        return {"intent": choice.intent.value, "confidence": choice.confidence}

    def _check_api(self, api_name: str) -> dict:
        if api_name in self.dep_symbols:
            return {"intent": "check_api", "api": api_name, "valid": True}

        parts = api_name.split(".")
        base = parts[0]
        suggestions: list[str] = []
        if self.dep_kb is not None:
            members = [k for k in self.dep_kb.all_names() if k.startswith(base + ".")]
            if members:
                suggestions = difflib.get_close_matches(api_name, members, n=3, cutoff=0.5)
                if not suggestions:
                    suggestions = members[:3]
        return {
            "intent": "check_api",
            "api": api_name,
            "valid": False,
            "reason": "not found in dependency API index",
            "suggestions": suggestions,
        }

    def check_call(self, call: str, file_path: str | None = None) -> dict:
        """Evaluate a single code snippet / API call for hallucination risk."""
        if "." in call:
            return self._check_api(call)

        if self._has_symbol(call):
            return {"call": call, "valid": True, "source": "project"}

        if call in self.dep_symbols:
            return {"call": call, "valid": True, "source": "dependency"}

        # Check whether the base module looks like a project module.
        # Compare against the full module path components, not a substring
        # ("util" must not match "pkg.utility").
        base = call.split("(")[0].split(".")[0].strip()
        if file_path and base in self._module_of(file_path).split("."):
            return {"call": call, "valid": True, "source": "same_module"}

        suggestions = difflib.get_close_matches(
            call, list(self.project_symbols) + list(self.dep_symbols), n=3
        )
        return {
            "call": call,
            "valid": False,
            "risk": "high",
            "suggestions": suggestions,
        }

    def _has_symbol(self, name: str) -> bool:
        """True when *name* matches a project symbol by full or trailing name.

        ``project_symbols`` is keyed by qualified name (``pkg.mod.helper``)
        while callers naturally ask about the short name (``helper``), so the
        trailing component is matched too. Generous on purpose: a false "valid"
        costs nothing, a false "risk: high" sends the agent chasing a ghost.
        """
        if name in self.project_symbols:
            return True
        suffix = f".{name}"
        return any(key.endswith(suffix) for key in self.project_symbols)

    def _module_of(self, file_path: str | Path) -> str:
        """Infer a Python module name from a file path relative to the project root.

        Falls back to a best-effort absolute-path derivation when no root is set.
        """
        p = Path(file_path)

        # Prefer project-relative path.
        if self.project_root:
            try:
                p.resolve().relative_to(self.project_root.resolve())
                return module_name_for(p, self.project_root)
            except ValueError:
                pass

        parts = list(p.with_suffix("").parts)
        return ".".join(parts)
