"""Integration-style tests for MCP tools and cross-file analysis."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest

from honestcode.mcp.tools import (
    check_api,
    check_symbol,
    explore_call_graph,
    find_dead_code,
    index_project,
    load_package_apis,
    load_project_deps,
    scan_file,
    validate_types,
)


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
    (tmp_path / "pkg" / "unused.py").write_text("def orphan():\n    return 42\n", encoding="utf-8")
    return tmp_path


def test_index_and_check_symbol(sample_project: Path):
    res = index_project(str(sample_project), force_rebuild=True)
    assert res["success"] is True
    assert res["symbols_indexed"] >= 3

    sym = check_symbol("pkg.core.main")
    assert sym["defined"] is True
    assert sym["location"]["file"] == "pkg/core.py"


def test_scan_file_undefined_call(sample_project: Path):
    bad = sample_project / "bad.py"
    bad.write_text("nonexistent_func()\n", encoding="utf-8")
    index_project(str(sample_project), force_rebuild=True)
    res = scan_file(str(bad))
    assert res["success"] is True
    assert any(i["name"] == "nonexistent_func" for i in res["issues"])


def test_validate_types_catches_none_iteration():
    res = validate_types("for x in None:\n    pass\n")
    assert res["success"] is True
    assert any(i["type"] == "none_iteration" for i in res["issues"])


def test_validate_types_catches_bad_builtin_argc():
    res = validate_types("len()\n")
    assert res["success"] is True
    assert any(i["type"] == "arg_count" for i in res["issues"])


def test_validate_types_accepts_valid_code():
    res = validate_types("x = [1, 2, 3]\nprint(len(x))\n")
    assert res["success"] is True
    assert res["issues"] == []


def test_load_package_apis():
    count = load_package_apis("math")
    assert count["success"] is True
    assert count["api_count"] > 0


def test_check_api_known():
    load_package_apis("math")
    res = check_api("math.sqrt")
    assert res["valid"] is True


def test_check_api_unknown_suggestion():
    load_package_apis("math")
    res = check_api("math.sqrtt")
    assert res["valid"] is False


def test_check_api_does_not_import_workspace_module(tmp_path: Path, monkeypatch):
    """check_api must not import a bare file that merely sits on sys.path.

    Importing executes code. An agent-written module in the workspace is
    importable from the server's cwd but is not an installed distribution, so
    answering ``check_api("name.attr")`` used to run agent-authored code inside
    the server process. Distribution metadata is read without executing.
    """
    probe = tmp_path / "hc_probe_not_a_distribution.py"
    probe.write_text(
        "import pathlib\npathlib.Path(str(pathlib.Path(__file__)) + '.IMPORTED').write_text('x')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    res = check_api("hc_probe_not_a_distribution.anything")
    assert res["success"] is True
    assert res.get("valid") is False
    assert res.get("reason") == "unknown module"
    assert not probe.with_name(probe.name + ".IMPORTED").exists()


def test_check_api_still_answers_stdlib_without_prelude():
    """A fresh process can ask about the stdlib — the import gate allows it."""
    from honestcode.mcp.tools import reset_dependency_state

    reset_dependency_state()
    res = check_api("math.sqrt")
    assert res["valid"] is True


def test_explore_call_graph(sample_project: Path):
    index_project(str(sample_project), force_rebuild=True)
    res = explore_call_graph("pkg.core.helper")
    assert res["success"] is True
    callers = {c["caller"] for c in res["callers"]}
    # Callers are module-qualified (pkg.core.main); accept short names too.
    assert any(c == "<module>" or c.endswith(".main") or c.endswith(".work") for c in callers)


def test_explore_call_graph_callees_by_qualified_name(sample_project: Path):
    """``main()`` calls ``helper()`` — callees of ``pkg.core.main`` must
    include ``helper``. This previously returned an empty list because the
    reference context was recorded as the short name ``main`` while the
    query used the qualified name ``pkg.core.main``.
    """
    index_project(str(sample_project), force_rebuild=True)
    res = explore_call_graph("pkg.core.main")
    assert res["success"] is True
    callee_names = {c["callee"] for c in res["callees"]}
    assert "helper" in callee_names, f"helper should be a callee of main; got {callee_names}"


def test_explore_call_graph_callees_short_name_fallback(sample_project: Path):
    """Short-name lookup must still resolve callers (the reference graph
    stores references by the called name, so ``helper``'s callers can be
    found via the short name even though ``main``'s own definitions are only
    stored under the qualified name ``pkg.core.main``).
    """
    index_project(str(sample_project), force_rebuild=True)
    res = explore_call_graph("helper")
    assert res["success"] is True
    callers = {c["caller"] for c in res["callers"]}
    assert "pkg.core.main" in callers or "pkg.core.Worker.work" in callers


def test_explore_call_graph_class_method_callees(sample_project: Path):
    """``Worker.work()`` calls ``helper()`` — callees of
    ``pkg.core.Worker.work`` must include ``helper``.
    """
    index_project(str(sample_project), force_rebuild=True)
    res = explore_call_graph("pkg.core.Worker.work")
    assert res["success"] is True
    callee_names = {c["callee"] for c in res["callees"]}
    assert "helper" in callee_names, f"helper should be a callee of work; got {callee_names}"


def test_find_dead_code(sample_project: Path):
    index_project(str(sample_project), force_rebuild=True)
    res = find_dead_code(entrypoints=["pkg.core.main"])
    assert res["success"] is True
    dead_names = {d["symbol"] for d in res["dead_symbols"]}
    assert "pkg.unused.orphan" in dead_names
    assert "pkg.core.main" not in dead_names


def test_load_project_deps(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text("pytest>=7.0\n# comment\n-r other.txt\n", encoding="utf-8")
    res = load_project_deps(str(tmp_path))
    assert res["success"] is True
    assert "pytest" in res["packages_loaded"]


def test_load_project_deps_pyproject(tmp_path: Path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        '[project]\ndependencies = ["pytest>=7.0", "requests==2.28"]\n',
        encoding="utf-8",
    )
    res = load_project_deps(str(tmp_path))
    assert res["success"] is True
    assert "pytest" in res["packages_loaded"]
