"""
Command-line interface for HonestCode.

Every MCP tool has a CLI equivalent so the same capabilities can be used
from scripts, CI pipelines, and non-MCP harnesses without launching a
server process.

Usage:
    honestcode index /path/to/project
    honestcode deps /path/to/project
    honestcode scan src/main.py
    honestcode check-symbol pkg.core.helper
    honestcode check-api pandas.read_csv
    honestcode validate "for x in None: pass"
    honestcode dead-code --entrypoints pkg.cli.main
    honestcode search "def \\w+_helper"
    honestcode call-graph pkg.core.helper
    honestcode similar --threshold 0.85
    honestcode stats
    honestcode choose-tool "is my_symbol defined"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import TYPE_CHECKING, Any

from honestcode.honest import project_binding as _binding
from honestcode.mcp import tools as _tools

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["main"]


def _emit(result: dict[str, Any]) -> int:
    """Print *result* as JSON to stdout and return an exit code."""
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    # Non-zero exit when the operation reported a failure.
    return 0 if result.get("success", True) else 1


def _emit_issues(result: dict[str, Any], issue_key: str = "issues") -> int:
    """Like _emit but exit non-zero when issues were found."""
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    if not result.get("success", True):
        return 1
    n = len(result.get(issue_key, []))
    return 1 if n else 0


def _cmd_index(args: argparse.Namespace) -> int:
    return _emit(_tools.index_project(args.path, force_rebuild=args.force, watch=args.watch))


def _cmd_affected(args: argparse.Namespace) -> int:
    return _emit(_tools.affected_files(base=args.base, head=args.head, max_depth=args.depth))


def _cmd_impact(args: argparse.Namespace) -> int:
    return _emit(_tools.explore_impact(args.symbol, max_depth=args.depth))


def _cmd_watch(args: argparse.Namespace) -> int:
    """Index and keep the project fresh until interrupted (Ctrl+C)."""
    res = _tools.index_project(args.path, force_rebuild=args.force, watch=True)
    _emit(res)
    if not res.get("success"):
        return 1
    print(f"Watching {args.path} — press Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _emit(_tools.stop_watching())
        return 0


def _cmd_init(args: argparse.Namespace) -> int:
    return _emit(_binding.init_project(args.path, force=args.force))


def _cmd_uninit(args: argparse.Namespace) -> int:
    return _emit(_binding.uninit_project(args.path))


def _cmd_install(args: argparse.Namespace) -> int:
    return _emit(
        _binding.install_mcp_config(
            path=args.path,
            client=args.client,
            dry_run=args.dry_run,
        )
    )


def _cmd_root(args: argparse.Namespace) -> int:
    root = _binding.find_project_root(args.path)
    if root is None:
        return _emit(
            {"success": False, "error": "No project root found (no .honestcode/ or .git/)"}
        )
    return _emit({"success": True, "root": str(root)})


def _cmd_deps(args: argparse.Namespace) -> int:
    return _emit(_tools.load_project_deps(args.path))


def _cmd_scan(args: argparse.Namespace) -> int:
    report = _tools.scan_file(args.file)
    if not report.get("success", True):
        return _emit(report)
    if getattr(args, "format", "json") == "text":
        from honestcode.verify.evidence import render_text

        sys.stdout.write(render_text(report) + "\n")
        return 1 if report["status"] == "fail" else 0
    return _emit_issues(report, issue_key="issues")


def _cmd_verify(args: argparse.Namespace) -> int:
    report = _tools.verify_file(args.file)
    if not report.get("success", True):
        return _emit(report)
    sys.stdout.write(report["text"] + "\n")
    return 1 if report["status"] == "fail" else 0


def _cmd_check_symbol(args: argparse.Namespace) -> int:
    res = _tools.check_symbol(args.symbol, file_path=args.file or None)
    _emit(res)
    return 0 if res.get("defined") else 1


def _cmd_check_api(args: argparse.Namespace) -> int:
    res = _tools.check_api(args.api)
    _emit(res)
    return 0 if res.get("valid") else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    return _emit_issues(_tools.validate_types(args.code), issue_key="issues")


def _cmd_execute(args: argparse.Namespace) -> int:
    return _emit(_tools.execute_code(args.code, prelude=args.prelude, known_names=args.known_names))


def _cmd_dead_code(args: argparse.Namespace) -> int:
    return _emit_issues(
        _tools.find_dead_code(
            entrypoints=args.entrypoints,
            ignore_patterns=args.ignore,
            include_tests=args.include_tests,
        ),
        issue_key="dead_symbols",
    )


def _cmd_similar(args: argparse.Namespace) -> int:
    return _emit_issues(_tools.find_similar_code(threshold=args.threshold), issue_key="pairs")


def _cmd_call_graph(args: argparse.Namespace) -> int:
    return _emit(_tools.explore_call_graph(args.symbol))


def _cmd_search(args: argparse.Namespace) -> int:
    return _emit_issues(_tools.search_code(args.pattern, glob=args.glob), issue_key="matches")


def _cmd_load_package(args: argparse.Namespace) -> int:
    return _emit(_tools.load_package_apis(args.package))


def _cmd_stats(args: argparse.Namespace) -> int:
    return _emit(_tools.get_project_stats())


def _cmd_choose_tool(args: argparse.Namespace) -> int:
    return _emit(_tools.choose_tool(args.query))


def _version_string() -> str:
    try:
        from importlib.metadata import version

        return f"honestcode {version('honestcode')}"
    except Exception:  # noqa: BLE001 - package metadata unavailable (e.g. source tree)
        from honestcode import __version__

        return f"honestcode {__version__}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="honestcode",
        description="HonestCode CLI — verify AI-generated code against your project.",
    )
    parser.add_argument("--version", action="version", version=_version_string())
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("index", help="Build (or reuse) the project symbol index.")
    p.add_argument("path", help="Project root directory.")
    p.add_argument("--force", action="store_true", help="Force rebuild the index.")
    p.add_argument(
        "--watch",
        action="store_true",
        help="Start a background watcher that re-indexes on file changes.",
    )
    p.set_defaults(func=_cmd_index)

    p = sub.add_parser(
        "watch",
        help="Index a project and keep it fresh (background watcher, Ctrl+C to stop).",
    )
    p.add_argument("path", help="Project root directory.")
    p.add_argument("--force", action="store_true", help="Force rebuild the index.")
    p.set_defaults(func=_cmd_watch)

    p = sub.add_parser("deps", help="Load dependency APIs from requirements.txt / pyproject.toml.")
    p.add_argument("path", help="Project root directory.")
    p.set_defaults(func=_cmd_deps)

    p = sub.add_parser("scan", help="Scan a file for undefined calls and API issues.")
    p.add_argument("file", help="Path to the Python file to scan.")
    p.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("verify", help="Verify a file and print agent-readable evidence.")
    p.add_argument("file", help="Path to the Python file to verify.")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("check-symbol", help="Check whether a symbol is defined in the project.")
    p.add_argument("symbol", help="Module-qualified symbol name, e.g. pkg.core.helper.")
    p.add_argument("--file", default="", help="Optional file the symbol appears in.")
    p.set_defaults(func=_cmd_check_symbol)

    p = sub.add_parser("check-api", help="Verify a library API call exists.")
    p.add_argument("api", help="Fully qualified API name, e.g. pandas.read_csv.")
    p.set_defaults(func=_cmd_check_api)

    p = sub.add_parser("validate", help="Run structural type checks on a code snippet.")
    p.add_argument("code", help="Python source to validate.")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("execute", help="Execute code in a sandboxed subprocess.")
    p.add_argument("code", help="Python source to execute.")
    p.add_argument("--prelude", default="", help="Setup code run before the main code.")
    p.add_argument(
        "--known-names",
        nargs="*",
        default=None,
        help="Known symbol names for typo suggestions.",
    )
    p.set_defaults(func=_cmd_execute)

    p = sub.add_parser(
        "dead-code",
        help="Find unused symbols. Exit code 1 if any dead symbols are found.",
    )
    p.add_argument(
        "--entrypoints",
        nargs="*",
        default=None,
        help="Symbol names to always treat as alive.",
    )
    p.add_argument(
        "--ignore",
        nargs="*",
        default=None,
        help="Regex patterns for files to exclude from dead-code results.",
    )
    p.add_argument(
        "--no-tests",
        dest="include_tests",
        action="store_false",
        help="Do not treat symbols in test files as alive.",
    )
    p.set_defaults(func=_cmd_dead_code, include_tests=True)

    p = sub.add_parser("similar", help="Find function-level code clones across the project.")
    p.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Similarity ratio above which two functions are reported (0-1).",
    )
    p.set_defaults(func=_cmd_similar)

    p = sub.add_parser("call-graph", help="Explore callers and callees of a symbol.")
    p.add_argument("symbol", help="Symbol name to explore.")
    p.set_defaults(func=_cmd_call_graph)

    p = sub.add_parser("impact", help="Compute the blast radius of a symbol.")
    p.add_argument("symbol", help="Symbol name to analyze.")
    p.add_argument("--depth", type=int, default=3, help="Max call-graph hops (default: 3).")
    p.set_defaults(func=_cmd_impact)

    p = sub.add_parser(
        "affected",
        help="Files/tests that may be affected by a git diff (from the call graph).",
    )
    p.add_argument("--base", default="HEAD", help="Git ref to diff against (default: HEAD).")
    p.add_argument("--head", default=None, help="Optional second ref for base..head diffs.")
    p.add_argument("--depth", type=int, default=4, help="Max call-graph hops (default: 4).")
    p.set_defaults(func=_cmd_affected)

    p = sub.add_parser("search", help="Search project source code with a regex pattern.")
    p.add_argument("pattern", help="Regular expression to search for.")
    p.add_argument("--glob", default="*.py", help="Glob pattern for files to search.")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("load-package", help="Load API signatures for a specific package.")
    p.add_argument("package", help="Importable package name, e.g. numpy.")
    p.set_defaults(func=_cmd_load_package)

    p = sub.add_parser("stats", help="Show statistics about the indexed project.")
    p.set_defaults(func=_cmd_stats)

    p = sub.add_parser("choose-tool", help="Show which tool a natural-language query maps to.")
    p.add_argument("query", help="Natural-language query.")
    p.set_defaults(func=_cmd_choose_tool)

    p = sub.add_parser(
        "init",
        help="Bind a directory as a HonestCode project (.honestcode/).",
    )
    p.add_argument("path", nargs="?", default=None, help="Project directory (default: cwd).")
    p.add_argument("--force", action="store_true", help="Re-create an existing binding.")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("uninit", help="Remove the .honestcode/ project binding.")
    p.add_argument("path", nargs="?", default=None, help="Project directory (default: cwd).")
    p.set_defaults(func=_cmd_uninit)

    p = sub.add_parser(
        "install",
        help="Register the MCP server with a client (Cursor, VS Code, Claude Code).",
    )
    p.add_argument("path", nargs="?", default=None, help="Project directory (default: cwd).")
    p.add_argument(
        "--client",
        choices=["cursor", "vscode", "claude"],
        default=None,
        help="Only configure this client (default: all detected).",
    )
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing.")
    p.set_defaults(func=_cmd_install)

    p = sub.add_parser("root", help="Print the detected project root (binding or git).")
    p.add_argument("path", nargs="?", default=None, help="Start directory (default: cwd).")
    p.set_defaults(func=_cmd_root)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
