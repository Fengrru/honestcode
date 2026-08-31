"""
HonestCode MCP Server.

A Model Context Protocol server that exposes hallucination-detection tools
so any MCP-compatible client (Cursor, Claude Desktop, VS Code, etc.) can
verify generated code against the real project structure and dependencies.

Run with:
    python -m honestcode.mcp.server            # stdio (default)
    python -m honestcode.mcp.server --sse       # SSE transport
    python -m honestcode.mcp.server --sse --host 0.0.0.0 --port 8000

Tool visibility is controlled by the ``HONESTCODE_TOOLS`` environment variable.
By default only ``scan_file`` is exposed; set ``HONESTCODE_TOOLS`` to a
comma-separated list of tool names (or ``all``) to expose more:

    export HONESTCODE_TOOLS=index,check_symbol,check_api,validate_types
    export HONESTCODE_TOOLS=all
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from honestcode.mcp import tools as _tools

if TYPE_CHECKING:
    from collections.abc import Callable

logging.basicConfig(level=os.environ.get("HONESTCODE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("honestcode.mcp")

mcp = FastMCP("honestcode")


def _mcp_index_project(root_path: str, force_rebuild: bool = False, watch: bool = False) -> dict:
    """Build (or reuse) the symbol index for a project directory.

    Call this once before other tools. Subsequent calls return a cached index
    unless force_rebuild=True.

    Args:
        root_path: Absolute path to the project root.
        force_rebuild: If True, discard the cache and rebuild the index.
        watch: If True, start a background watcher that re-indexes on changes.
    """
    logger.info("index_project(%s, force_rebuild=%s, watch=%s)", root_path, force_rebuild, watch)
    return _tools.index_project(root_path, force_rebuild=force_rebuild, watch=watch)


def _mcp_load_project_deps(root_path: str) -> dict:
    """Load dependency APIs declared in requirements.txt / pyproject.toml
    so library calls can be validated.

    Args:
        root_path: Absolute path to the project root.
    """
    return _tools.load_project_deps(root_path)


def _mcp_check_symbol(symbol_name: str, file_path: str = "") -> dict:
    """Check whether a symbol is actually defined in the project.

    Args:
        symbol_name: The identifier to verify (function/class/variable name).
        file_path: Optional file the symbol appears in.
    """
    return _tools.check_symbol(symbol_name, file_path or None)


def _mcp_check_api(api_name: str) -> dict:
    """Verify a library API call is correct, e.g. pd.read_exel -> read_excel.

    Args:
        api_name: Fully qualified API name, e.g. "pandas.read_csv".
    """
    return _tools.check_api(api_name)


def _mcp_execute_code(code: str, prelude: str = "", known_names: list[str] | None = None) -> dict:
    """Execute code in a sandbox and return stdout/stderr plus structured
    error analysis with fix suggestions.

    Args:
        code: Python source to execute.
        prelude: Optional setup code (imports) run before `code`.
        known_names: Optional list of known symbol names for typo suggestions.
    """
    return _tools.execute_code(code, prelude, known_names)


def _mcp_scan_file(file_path: str) -> dict:
    """Scan a file for potential hallucinations: undefined symbols, missing
    imports, and incorrect API calls.

    Args:
        file_path: Absolute path to the Python file.
    """
    return _tools.scan_file(file_path)


def _mcp_verify_file(file_path: str) -> dict:
    """Verify a file and return structured evidence for the agent.

    Returns ``status``, ``findings`` with ``evidence``, and a ``text`` summary.

    Args:
        file_path: Absolute path to the Python file.
    """
    return _tools.verify_file(file_path)


def _mcp_load_package_apis(package_name: str) -> dict:
    """Load (and cache) API signatures for a specific installed package.

    Args:
        package_name: Importable package name, e.g. "numpy".
    """
    return _tools.load_package_apis(package_name)


def _mcp_get_project_stats() -> dict:
    """Return statistics about the currently indexed project."""
    return _tools.get_project_stats()


def _mcp_validate_types(code: str) -> dict:
    """Run a lightweight AST-based structural check on code.

    Args:
        code: Python source to analyze.
    """
    return _tools.validate_types(code)


def _mcp_find_dead_code(
    entrypoints: list[str] | None = None,
    ignore_patterns: list[str] | None = None,
    include_tests: bool = True,
) -> dict:
    """Find symbols that appear to be unused in the indexed project.

    Args:
        entrypoints: Symbol names to always treat as alive (e.g. ["main", "cli"]).
        ignore_patterns: Regex patterns for files to exclude from dead-code results.
        include_tests: Treat symbols in test files as alive.
    """
    return _tools.find_dead_code(entrypoints, ignore_patterns, include_tests)


def _mcp_find_similar_code(threshold: float = 0.85) -> dict:
    """Find function-level code clones across the project.

    Args:
        threshold: Similarity ratio above which two functions are reported (0-1).
    """
    return _tools.find_similar_code(threshold)


def _mcp_explore_call_graph(symbol_name: str) -> dict:
    """Explore callers and callees of a symbol across the project.

    Args:
        symbol_name: Name of the symbol to explore.
    """
    return _tools.explore_call_graph(symbol_name)


def _mcp_explore_impact(symbol_name: str, max_depth: int = 3) -> dict:
    """Compute the blast radius of a symbol: transitively impacted symbols/files.

    Args:
        symbol_name: Name of the symbol to analyze.
        max_depth: Maximum number of call-graph hops to follow.
    """
    return _tools.explore_impact(symbol_name, max_depth)


def _mcp_stop_watching() -> dict:
    """Stop the background file watcher, if any."""
    return _tools.stop_watching()


def _mcp_affected_files(base: str = "HEAD", head: str | None = None, max_depth: int = 4) -> dict:
    """Find files (especially tests) that may be affected by a git diff.

    Args:
        base: Git ref to diff against (default: HEAD).
        head: Optional second ref; when given, diffs base..head.
        max_depth: Maximum call-graph hops to trace.
    """
    return _tools.affected_files(base, head, max_depth)


def _mcp_search_code(pattern: str, glob: str = "*.py") -> dict:
    """Search project source code with a regex pattern.

    Args:
        pattern: Regular expression to search for.
        glob: Glob pattern for files to search.
    """
    return _tools.search_code(pattern, glob)


def _mcp_choose_tool(query: str) -> dict:
    """For debugging: choose which tool a natural-language query maps to."""
    return _tools.choose_tool(query)


# ── Tool registry table ────────────────────────────────────────────────
# Maps the public tool name (used by MCP clients and the HONESTCODE_TOOLS
# env var) to its wrapper function and short description.
_ALL_TOOLS: dict[str, tuple[Callable, str]] = {
    "scan_file": (_mcp_scan_file, "Scan a file for undefined calls and API issues."),
    "verify_file": (_mcp_verify_file, "Verify a file and return structured agent evidence."),
    "index": (_mcp_index_project, "Build the project symbol index."),
    "deps": (_mcp_load_project_deps, "Load dependency APIs."),
    "check_symbol": (_mcp_check_symbol, "Verify a symbol is defined in the project."),
    "check_api": (_mcp_check_api, "Verify a library API call exists."),
    "execute_code": (_mcp_execute_code, "Run code in a sandbox."),
    "validate_types": (_mcp_validate_types, "Structural type checks."),
    "find_dead_code": (_mcp_find_dead_code, "Find unused symbols."),
    "find_similar_code": (_mcp_find_similar_code, "Find code clones."),
    "explore_call_graph": (_mcp_explore_call_graph, "Explore callers/callees."),
    "explore_impact": (_mcp_explore_impact, "Blast radius of a symbol."),
    "affected_files": (_mcp_affected_files, "Files/tests affected by a git diff."),
    "stop_watching": (_mcp_stop_watching, "Stop the background file watcher."),
    "search_code": (_mcp_search_code, "Regex search across project files."),
    "load_package_apis": (_mcp_load_package_apis, "Load a package's API signatures."),
    "get_project_stats": (_mcp_get_project_stats, "Return index statistics."),
    "choose_tool": (_mcp_choose_tool, "Map a query to the best tool."),
}

# ``scan_file`` is always exposed; the remaining tools are opt-in.
_PRIMARY_TOOL = "scan_file"


def _resolve_tool_whitelist() -> set[str]:
    """Resolve which tool names to expose on this server.

    Reads ``HONESTCODE_TOOLS``. If unset, only the primary tool
    (``scan_file``) is exposed. ``all`` exposes every tool. Otherwise the
    value is treated as a comma-separated list of names; unknown names are
    ignored with a warning.
    """
    raw = os.environ.get("HONESTCODE_TOOLS", "").strip()
    if not raw:
        return {_PRIMARY_TOOL}

    if raw.lower() == "all":
        return set(_ALL_TOOLS.keys())

    requested = {t.strip() for t in raw.split(",") if t.strip()}
    unknown = requested - set(_ALL_TOOLS.keys())
    for name in unknown:
        logger.warning("HONESTCODE_TOOLS: unknown tool '%s' ignored", name)
    # The primary tool is always included even if not explicitly listed.
    requested.add(_PRIMARY_TOOL)
    return requested & set(_ALL_TOOLS.keys())


def _register_tools() -> None:
    """Register the whitelisted tools into FastMCP.

    Idempotent: safe to call multiple times; already-registered tools are
    skipped to avoid duplicate registration errors.
    """
    already = set(getattr(mcp, "_registered_tool_names", set()))  # type: ignore[attr-defined]
    whitelist = _resolve_tool_whitelist()
    for name in whitelist:
        if name in already:
            continue
        func, _desc = _ALL_TOOLS[name]
        # FastMCP infers the schema from the function signature; pass the
        # public tool name explicitly so it shows up as `name`.
        mcp.tool(name=name)(func)
        already.add(name)
    mcp._registered_tool_names = already  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="honestcode-mcp",
        description="HonestCode MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="SSE host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="SSE port (default: 8000).")
    args = parser.parse_args(argv)

    _register_tools()

    whitelist = _resolve_tool_whitelist()
    logger.info(
        "Starting HonestCode MCP server (transport=%s, tools=%s)",
        args.transport,
        sorted(whitelist),
    )
    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
