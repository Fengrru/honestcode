"""Tests for the MCP server layer: tool registration, HONESTCODE_TOOLS
whitelist, SSE/stdio argument parsing, and shared AST utilities."""

from __future__ import annotations

import ast
import os
from typing import TYPE_CHECKING

import pytest

from honestcode.mcp import server as server_mod
from honestcode.mcp.tools import check_symbol, validate_types
from honestcode.structure.utils import call_base, call_name

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def sample_project(tmp_path: Path):
    """Create a tiny project with a few functions and a class."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "def helper():\n    return 1\n\n"
        "def main():\n    return helper()\n\n"
        "class Worker:\n    def work(self):\n        return helper()\n",
        encoding="utf-8",
    )
    return tmp_path


# ── Tool registry / whitelist ──────────────────────────────────────────
def test_default_whitelist_is_scan_file_only():
    # Simulate an unset env var.
    old = os.environ.pop("HONESTCODE_TOOLS", None)
    try:
        wl = server_mod._resolve_tool_whitelist()
        assert wl == {"scan_file"}
    finally:
        if old is not None:
            os.environ["HONESTCODE_TOOLS"] = old


def test_all_keyword_exposes_every_tool(monkeypatch):
    monkeypatch.setenv("HONESTCODE_TOOLS", "all")
    wl = server_mod._resolve_tool_whitelist()
    assert "scan_file" in wl
    assert "index" in wl
    assert "check_api" in wl
    assert len(wl) == len(server_mod._ALL_TOOLS)


def test_explicit_list_includes_scan_file_implicitly(monkeypatch):
    monkeypatch.setenv("HONESTCODE_TOOLS", "index,check_symbol")
    wl = server_mod._resolve_tool_whitelist()
    assert wl == {"scan_file", "index", "check_symbol"}


def test_unknown_tool_names_ignored(monkeypatch):
    monkeypatch.setenv("HONESTCODE_TOOLS", "scan_file,nonexistent_xyz")
    wl = server_mod._resolve_tool_whitelist()
    assert "nonexistent_xyz" not in wl
    assert "scan_file" in wl


def test_registry_contains_all_tools():
    expected = {
        "scan_file",
        "index",
        "deps",
        "check_symbol",
        "check_api",
        "execute_code",
        "validate_types",
        "find_dead_code",
        "find_similar_code",
        "explore_call_graph",
        "explore_impact",
        "affected_files",
        "stop_watching",
        "search_code",
        "load_package_apis",
        "get_project_stats",
        "choose_tool",
        "verify_file",
    }
    assert set(server_mod._ALL_TOOLS.keys()) == expected


# ── Tool wrappers (end-to-end through the registry) ────────────────────
def test_scan_file_wrapper(sample_project: Path, monkeypatch):
    # Ensure only scan_file is exposed; the wrapper should still work.
    monkeypatch.delenv("HONESTCODE_TOOLS", raising=False)
    bad = sample_project / "bad.py"
    bad.write_text("nonexistent_func()\n", encoding="utf-8")
    server_mod._mcp_index_project(str(sample_project))
    res = server_mod._mcp_scan_file(str(bad))
    assert res["success"] is True
    assert any(i["name"] == "nonexistent_func" for i in res["issues"])


def test_scan_file_missing_path_wrapper():
    res = server_mod._mcp_scan_file("/nonexistent/file.py")
    assert res["success"] is False


def test_validate_types_wrapper():
    res = server_mod._mcp_validate_types("for x in None:\n    pass\n")
    assert res["success"] is True
    assert any(i["type"] == "none_iteration" for i in res["issues"])


def test_choose_tool_wrapper():
    res = server_mod._mcp_choose_tool("check if my_symbol is defined")
    assert res["tool"] == "check_symbol"


def test_check_api_wrapper(monkeypatch):
    server_mod._mcp_load_package_apis("math")
    res = server_mod._mcp_check_api("math.sqrt")
    assert res["valid"] is True
    res = server_mod._mcp_check_api("math.sqrtt")
    assert res["valid"] is False
    assert res.get("suggestion")


def test_index_project_missing_path_wrapper():
    res = server_mod._mcp_index_project("/nonexistent/path/xyz")
    assert res["success"] is False
    assert "does not exist" in res["error"]


# ── Utils ──────────────────────────────────────────────────────────────
def test_call_name_name():
    node = ast.parse("foo()").body[0].value.func  # type: ignore[attr-defined]
    assert call_name(node) == "foo"


def test_call_name_attribute_chain():
    node = ast.parse("obj.attr.method()").body[0].value.func  # type: ignore[attr-defined]
    assert call_name(node) == "obj.attr.method"


def test_call_name_complex_expr():
    node = ast.parse("(a + b).foo()").body[0].value.func  # type: ignore[attr-defined]
    assert call_name(node) is None


def test_call_name_none():
    assert call_name(ast.Constant(value=1)) is None


def test_call_base():
    assert call_base("pkg.module.func") == "pkg"
    assert call_base("func") == "func"


def test_validate_types_bad_builtin_kwarg():
    res = validate_types("len(x=1)\n")
    assert res["success"] is True
    assert any(i["type"] == "arg_count" for i in res["issues"])


def test_check_symbol_before_index(monkeypatch):
    monkeypatch.setattr("honestcode.mcp.tools._project_index", None)
    res = check_symbol("anything")
    assert res["success"] is False
    assert "indexed" in res["error"]
