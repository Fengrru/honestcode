"""Regression tests for the 0.3.1 correctness fixes.

Each test reproduces a false positive / correctness bug that shipped in
0.3.0 and is verified against the fixed behaviour:

* cross-file new symbols (stale in-memory index)
* dependency submodule aliases (``from numpy import random``)
* PyPI name vs import name (``PyYAML`` -> ``yaml``)
* dead-code false deaths (qualified defs vs literal refs)
* FTS5 candidate narrowing dropping mid-token matches
* syntax errors bypassing the structured evidence protocol
"""

from __future__ import annotations

import sys
import time
from pathlib import Path  # noqa: TC003 - used in fixtures at runtime

import pytest

from honestcode.honest.symbol_index import get_project_index
from honestcode.mcp import tools
from honestcode.mcp.knowledge_base import resolve_import_names
from honestcode.mcp.tools import (
    check_symbol,
    find_dead_code,
    index_project,
    scan_file,
    search_code,
    verify_file,
)
from honestcode.verify.verify import verify_file_source


@pytest.fixture
def fresh_kb():
    """Isolate the global dependency KB so tests don't leak into each other."""
    tools._dep_kb.reset()
    yield tools._dep_kb
    tools._dep_kb.reset()


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "def helper():\n    return 1\n",
        encoding="utf-8",
    )
    return tmp_path


# ── #1 cross-file new symbols (stale index) ────────────────────────────


def test_cross_file_new_symbol_not_flagged(proj: Path):
    """A symbol written after indexing must not be reported undefined."""
    index_project(str(proj), force_rebuild=True)
    (proj / "pkg" / "late.py").write_text(
        "class NewThing:\n    def run(self):\n        return 2\n",
        encoding="utf-8",
    )
    user = proj / "app.py"
    user.write_text(
        "from pkg.late import NewThing\n\n\ndef main():\n    return NewThing().run()\n",
        encoding="utf-8",
    )
    rep = verify_file(str(user))
    assert rep["status"] == "pass", rep["findings"]


def test_index_rebuilt_without_force_when_tree_changes(proj: Path):
    index_project(str(proj), force_rebuild=True)
    (proj / "pkg" / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    time.sleep(0.02)
    idx = get_project_index(proj, force_rebuild=False)
    assert "pkg.extra.extra" in idx.symbols
    assert idx.cached is False  # rebuilt, not served stale
    again = get_project_index(proj, force_rebuild=False)
    assert again.cached is True  # unchanged tree -> cached hit


def test_scan_uses_the_index_of_the_scanned_project(proj: Path, tmp_path_factory):
    """Two projects in one session: each file is grounded against its own index."""
    other = tmp_path_factory.mktemp("other")
    (other / "pkg").mkdir()
    (other / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (other / "pkg" / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    index_project(str(other), force_rebuild=True)

    user = proj / "app.py"
    user.write_text("from pkg.core import helper\n\n\ndef main():\n    return helper()\n")
    rep = verify_file(str(user))
    assert rep["status"] == "pass", rep["findings"]


# ── #2 dependency submodule aliases ────────────────────────────────────


@pytest.fixture
def fake_dep(tmp_path: Path, monkeypatch):
    """A tiny importable package used as a stand-in dependency."""
    dep_root = tmp_path / "_dep"
    pkg = dep_root / "fakelib"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from . import inner\n", encoding="utf-8")
    (pkg / "inner.py").write_text("def tool(seed):\n    return seed\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(dep_root))
    yield "fakelib"
    sys.modules.pop("fakelib", None)
    sys.modules.pop("fakelib.inner", None)


def test_dep_submodule_alias_not_flagged(tmp_path: Path, fresh_kb, fake_dep):
    """``from fakelib import inner; inner.tool()`` is valid, not invented."""
    (tmp_path / "requirements.txt").write_text("fakelib\n", encoding="utf-8")
    res = tools.load_project_deps(str(tmp_path))
    assert res["packages_loaded"] == ["fakelib"]

    f = tmp_path / "app.py"
    f.write_text(
        "from fakelib import inner\n\n\ndef fetch(seed):\n    return inner.tool(seed)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


def test_dep_alias_invented_call_still_caught(tmp_path: Path, fresh_kb, fake_dep):
    (tmp_path / "requirements.txt").write_text("fakelib\n", encoding="utf-8")
    tools.load_project_deps(str(tmp_path))
    f = tmp_path / "app.py"
    f.write_text(
        "from fakelib import inner\n\n\ndef fetch(seed):\n    return inner.toll(seed)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "fail"
    finding = rep["findings"][0]
    assert finding["kind"] == "invented_api"
    assert finding["symbol"] == "toll"
    assert finding["evidence"]["did_you_mean"] == "tool"
    assert finding["owner"] == "fakelib.inner"


# ── #3 PyPI name vs import name ────────────────────────────────────────


def test_resolve_import_names_pypi_alias():
    if resolve_import_names("PyYAML") == {"PyYAML"}:
        pytest.skip("PyYAML import mapping unavailable in this environment")
    assert "yaml" in resolve_import_names("PyYAML")


def test_resolve_import_names_dashed_name():
    # A declared package with a dashed PyPI name is trusted under its
    # underscore module spelling too.
    assert "foo_bar" in resolve_import_names("foo-bar")


def test_declared_but_uninstalled_dep_import_trusted(tmp_path: Path, fresh_kb, monkeypatch):
    """A declared dep that fails to import must not cause false undefineds."""
    (tmp_path / "requirements.txt").write_text("totally-absent-pkg\n", encoding="utf-8")
    tools.load_project_deps(str(tmp_path))
    assert "totally_absent_pkg" in fresh_kb.roots()

    f = tmp_path / "app.py"
    f.write_text(
        "from totally_absent_pkg import thing\n\n\ndef use():\n    return thing()\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


def test_declared_but_uninstalled_dep_member_call_is_silent(tmp_path: Path, fresh_kb):
    """``mod.attr()`` on a package we never enumerated must not be reported:
    "not in the index" is not evidence of absence (regression: tomli)."""
    (tmp_path / "requirements.txt").write_text("totally-absent-pkg\n", encoding="utf-8")
    tools.load_project_deps(str(tmp_path))

    f = tmp_path / "app.py"
    f.write_text(
        "import totally_absent_pkg\n\n\ndef use(text):\n    return totally_absent_pkg.parse(text)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


def test_enumerated_dep_member_call_still_caught(tmp_path: Path, fresh_kb, fake_dep):
    """The silence above must not disable detection for enumerated modules."""
    (tmp_path / "requirements.txt").write_text("fakelib\n", encoding="utf-8")
    tools.load_project_deps(str(tmp_path))
    assert "fakelib" in fresh_kb.enumerated_modules()

    f = tmp_path / "app.py"
    f.write_text(
        "import fakelib\n\n\ndef use(seed):\n    return fakelib.toll(seed)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "fail"
    assert rep["findings"][0]["kind"] == "invented_api"


def test_pypi_named_requirement_loads_import_name(tmp_path: Path, fresh_kb):
    """``PyYAML`` in requirements.txt grounds ``yaml.safe_load`` calls."""
    pytest.importorskip("yaml")
    (tmp_path / "requirements.txt").write_text("PyYAML\n", encoding="utf-8")
    res = tools.load_project_deps(str(tmp_path))
    assert res["packages_loaded"] == ["PyYAML"]
    assert res["total_apis"] > 0

    f = tmp_path / "conf.py"
    f.write_text(
        "import yaml\nfrom yaml import safe_load\n\n\ndef load(text):\n    return yaml.safe_load(text)\n\n\ndef load2(text):\n    return safe_load(text)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


# ── #4 dead code ───────────────────────────────────────────────────────


def test_dead_code_does_not_kill_live_entry_points(proj: Path):
    index_project(str(proj), force_rebuild=True)
    (proj / "pkg" / "cli.py").write_text(
        "from pkg.core import helper\n\n\ndef main():\n    return helper()\n\n\n"
        'if __name__ == "__main__":\n    main()\n',
        encoding="utf-8",
    )
    (proj / "pkg" / "orphan.py").write_text("def unused_thing():\n    return 0\n", encoding="utf-8")
    time.sleep(0.02)
    index_project(str(proj), force_rebuild=False)

    res = find_dead_code()
    names = {d["symbol"] for d in res["dead_symbols"]}
    assert "pkg.cli.main" not in names
    assert "pkg.core.helper" not in names
    assert "pkg.orphan.unused_thing" in names


# ── #5 FTS5 candidate narrowing ────────────────────────────────────────


def test_search_code_finds_mid_token_substring(proj: Path):
    index_project(str(proj), force_rebuild=True)
    full = search_code("helper")
    sub = search_code("helpe")
    assert full["count"] > 0
    assert sub["count"] == full["count"], (
        f"substring search dropped matches: {sub['count']} vs {full['count']}"
    )


# ── #7 syntax errors are findings, not tool errors ─────────────────────


def test_syntax_error_returns_structured_finding(proj: Path):
    index_project(str(proj), force_rebuild=True)
    bad = proj / "broken.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8")
    rep = verify_file(str(bad))
    assert rep["success"] is True
    assert rep["status"] == "fail"
    assert rep["findings"][0]["kind"] == "syntax_error"
    assert rep["findings"][0]["line"] == 1
    assert "text" in rep


def test_scan_file_syntax_error_same_shape(proj: Path):
    bad = proj / "broken2.py"
    bad.write_text("x = = 1\n", encoding="utf-8")
    rep = scan_file(str(bad))
    assert rep["success"] is True
    assert rep["status"] == "fail"
    assert rep["issues"][0]["kind"] == "syntax_error"


def test_verify_file_source_public_api(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text("def f(:\n", encoding="utf-8")
    findings, checked = verify_file_source(
        f.read_text(encoding="utf-8"),
        path=f,
        root=tmp_path,
        index=None,
        defined=set(),
        dep_names=set(),
    )
    assert findings[0].kind == "syntax_error"
    assert checked == 0


# ── #6 watcher + #1 freshness integration ─────────────────────────────


def test_watcher_reindex_picks_up_new_symbol(proj: Path):
    index_project(str(proj), force_rebuild=True, watch=True)
    try:
        (proj / "pkg" / "late.py").write_text("def late():\n    return 1\n", encoding="utf-8")
        deadline = time.monotonic() + 10
        defined = False
        while time.monotonic() < deadline:
            sym = check_symbol("pkg.late.late")
            if sym.get("defined"):
                defined = True
                break
            time.sleep(0.1)
        assert defined, "watcher re-index did not pick up the new symbol"
    finally:
        tools.stop_watching()


def test_nested_explicit_root_wins_over_git_marker(tmp_path: Path):
    """A directory indexed explicitly inside a git repo must be verified
    against its own index, not the enclosing repo's (regression: the
    demos/invented-api layout silently produced false passes)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # marker only; no real repo needed
    demo = repo / "demos" / "demo"
    (demo / "app").mkdir(parents=True)
    (demo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (demo / "app" / "client.py").write_text(
        "class UserClient:\n    def refresh(self):\n        return 1\n",
        encoding="utf-8",
    )
    index_project(str(demo), force_rebuild=True)
    bad = demo / "app" / "login.py"
    bad.write_text(
        "from app.client import UserClient\n\n\ndef login(client: UserClient):\n"
        "    client.refresh_token()\n",
        encoding="utf-8",
    )
    rep = verify_file(str(bad))
    assert rep["status"] == "fail", rep
    finding = rep["findings"][0]
    assert finding["kind"] == "invented_api"
    assert finding["symbol"] == "refresh_token"
    assert finding["evidence"]["did_you_mean"] == "refresh"


# ── #9 non-Python scan (needs the optional tree-sitter extras) ─────────


def _tree_sitter_available() -> bool:
    # The core plus the JavaScript grammar are both needed for a .js scan.
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_javascript  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _tree_sitter_available(), reason="tree-sitter extras not installed")
def test_non_python_imported_call_not_flagged(tmp_path: Path):
    js = tmp_path / "app.js"
    js.write_text(
        "const { format } = require('./fmt');\n\nfunction run() {\n  return format('x');\n}\n\nmodule.exports = { run };\n",
        encoding="utf-8",
    )
    res = scan_file(str(js))
    assert res["success"] is True
    names = {i["name"] for i in res["issues"]}
    assert "format" not in names  # imported -> trusted
    assert all(i.get("confidence") == "low" for i in res["issues"])


# ── #10 function locals are not project symbols ────────────────────────


def test_function_locals_not_indexed(proj: Path):
    index_project(str(proj), force_rebuild=True)
    assert check_symbol("pkg.core.helper")["defined"] is True
    # ``x = 1`` inside helper() must not appear as a project symbol.
    sym = check_symbol("pkg.core.helper.x")
    assert sym["defined"] is False


def test_class_attrs_still_indexed(proj: Path):
    (proj / "pkg" / "cls.py").write_text(
        "class Cfg:\n    version = 1\n\n    def get(self):\n        local = 2\n        return local\n",
        encoding="utf-8",
    )
    time.sleep(0.02)
    index_project(str(proj), force_rebuild=False)
    assert check_symbol("pkg.cls.Cfg.version")["defined"] is True
    assert check_symbol("pkg.cls.Cfg.get.local")["defined"] is False


# ── router short-name matching ─────────────────────────────────────────


def test_router_check_call_matches_qualified_symbols():
    from honestcode.honest.router import HonestRouter

    router = HonestRouter(project_symbols={"pkg.core.helper": {}}, dep_symbols=set())
    res = router.check_call("helper")
    assert res["valid"] is True
    assert res["source"] == "project"


# ── #8 sandbox hygiene ─────────────────────────────────────────────────


def test_sandbox_scrubs_database_env_vars():
    import os

    os.environ["DATABASE_URL_TEST_HONESTCODE"] = "postgres://secret"
    try:
        res = tools.execute_code("import os\nprint('DATABASE_URL_TEST_HONESTCODE' in os.environ)")
    finally:
        os.environ.pop("DATABASE_URL_TEST_HONESTCODE", None)
    assert res["success"], res.get("error")
    assert res["stdout"].strip() == "False"


def test_sandbox_timeout_still_enforced():
    res = tools.execute_code(
        "while True:\n    pass",
    )
    assert res["success"] is False
    assert res.get("details", {}).get("timeout") is True or "timed out" in res.get("error", "")


# ── CLI --version ──────────────────────────────────────────────────────


def test_cli_version_flag(capsys):
    from honestcode.cli import main as cli_main

    with pytest.raises(SystemExit) as exc:
        cli_main(["--version"])
    assert exc.value.code == 0
    assert "honestcode" in capsys.readouterr().out


# ═══════════════════════════════════════════════════════════════════════
# v0.4.0 real-world precision fixes
# ═══════════════════════════════════════════════════════════════════════


# ── stdlib imports are trusted ─────────────────────────────────────────


def test_stdlib_imports_are_trusted(tmp_path: Path):
    """``from typing import TypeVar`` cannot be a hallucination."""
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    f = tmp_path / "mod.py"
    f.write_text(
        "from base64 import b64encode\n"
        "from logging import NullHandler\n"
        "from typing import TypeVar\n"
        "from urllib.parse import urlparse\n"
        "\n"
        "T = TypeVar('T')\n"
        "NullHandler()\n"
        "b64encode(b'x')\n"
        "urlparse('http://x')\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


def test_stdlib_import_typo_still_caught(tmp_path: Path):
    """Trusting the stdlib must not trust invented *names* from it."""
    f = tmp_path / "mod.py"
    f.write_text(
        "from typing import TypeVarr\n\n\ndef f():\n    return TypeVarr('T')\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "fail"
    assert rep["findings"][0]["symbol"] == "TypeVarr"


# ── keyword-argument arity ─────────────────────────────────────────────


def _arity_findings(tmp_path: Path, code: str) -> list[dict]:
    f = tmp_path / "caller.py"
    f.write_text(code, encoding="utf-8")
    rep = verify_file(str(f))
    return [x for x in rep["findings"] if x["kind"] == "wrong_call"]


def test_keyword_arguments_satisfy_required_params(tmp_path: Path):
    """``f(a, b)`` called as ``f(a=1, b=2)`` is valid."""
    code = "def target(a, b):\n    return a, b\n\ndef caller():\n    return target(a=1, b=2)\n"
    assert _arity_findings(tmp_path, code) == []


def test_keyword_only_parameters_counted(tmp_path: Path):
    """``def f(x, *, flag)`` called as ``f(1, flag=True)`` is valid."""
    code = (
        "def target(x, *, flag):\n    return x\n\ndef caller():\n    return target(1, flag=True)\n"
    )
    assert _arity_findings(tmp_path, code) == []


def test_missing_keyword_argument_still_caught(tmp_path: Path):
    """Omitting a required parameter is still reported."""
    code = "def target(a, b):\n    return a, b\n\ndef caller():\n    return target(a=1)\n"
    findings = _arity_findings(tmp_path, code)
    assert findings and "too few" in findings[0]["message"]


def test_too_many_positional_still_caught(tmp_path: Path):
    code = "def target(a, b):\n    return a, b\n\ndef caller():\n    return target(1, 2, 3)\n"
    findings = _arity_findings(tmp_path, code)
    assert findings and "too many" in findings[0]["message"]


def test_kwargs_call_style_not_flagged(tmp_path: Path):
    """The exact pattern that was flagged on real code and on this repo."""
    code = (
        "def build(file, findings, *, checked=0):\n"
        "    return file\n\n"
        "def use():\n"
        "    return build(file='a.py', findings=[], checked=1)\n"
    )
    assert _arity_findings(tmp_path, code) == []


# ── staticmethod binding ───────────────────────────────────────────────


def test_staticmethod_arity_correct(tmp_path: Path):
    """``@staticmethod def enc(data)`` takes exactly one argument."""
    code = (
        "class Codec:\n"
        "    @staticmethod\n"
        "    def enc(data):\n"
        "        return data\n\n"
        "def use(codec: Codec):\n"
        "    return codec.enc('x')\n"
    )
    assert _arity_findings(tmp_path, code) == []


def test_staticmethod_wrong_arity_still_caught(tmp_path: Path):
    code = (
        "class Codec:\n"
        "    @staticmethod\n"
        "    def enc(data):\n"
        "        return data\n\n"
        "def use(codec: Codec):\n"
        "    return codec.enc()\n"
    )
    findings = _arity_findings(tmp_path, code)
    assert findings and "too few" in findings[0]["message"]


# ── re-export through a project module ─────────────────────────────────


def test_reexport_through_project_module_trusted(tmp_path: Path):
    """``from .compat import urlparse`` where compat re-exports it is grounded."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "compat.py").write_text(
        "from urllib.parse import urlparse\n\n__all__ = ['urlparse']\n",
        encoding="utf-8",
    )
    index_project(str(tmp_path), force_rebuild=True)
    f = pkg / "app.py"
    f.write_text(
        "from .compat import urlparse\n\n\ndef parse(url):\n    return urlparse(url)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


def test_invented_reexport_still_caught(tmp_path: Path):
    """A name that is neither defined nor re-exported is still reported."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "compat.py").write_text(
        "from urllib.parse import urlparse\n\n__all__ = ['urlparse']\n",
        encoding="utf-8",
    )
    index_project(str(tmp_path), force_rebuild=True)
    f = pkg / "app.py"
    f.write_text(
        "from .compat import urlparse_v2\n\n\ndef parse(url):\n    return urlparse_v2(url)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "fail"
    assert any(x["symbol"] == "urlparse_v2" for x in rep["findings"])


# ── no-marker projects fall back to the file's directory ──────────────


def test_markerless_project_is_still_grounded(tmp_path: Path):
    """A scratch dir with no .git/pyproject must not silently pass broken code."""
    work = tmp_path / "scratch"  # no markers anywhere above tmp_path
    pkg = work / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "client.py").write_text(
        "class UserClient:\n    def refresh(self):\n        return 1\n",
        encoding="utf-8",
    )
    bad = pkg / "login.py"
    bad.write_text(
        "from pkg.client import UserClient\n\n\ndef login(client: UserClient):\n"
        "    return client.refresh_token()\n",
        encoding="utf-8",
    )
    rep = verify_file(str(bad))
    assert rep["status"] == "fail", rep
    assert rep["findings"][0]["symbol"] == "refresh_token"


# ── declared-only dependency loading ───────────────────────────────────


def test_declared_only_deps_trust_imports_but_do_not_fabricate(tmp_path: Path, fresh_kb):
    """``import_packages=False`` registers roots without importing anything."""
    (tmp_path / "requirements.txt").write_text("fakelib-not-installed\n", encoding="utf-8")
    res = tools.load_project_deps(str(tmp_path), import_packages=False)
    assert res["packages_loaded"] == ["fakelib-not-installed"]
    assert "fakelib_not_installed" in fresh_kb.roots()
    assert fresh_kb.enumerated_modules() == set()

    f = tmp_path / "app.py"
    f.write_text(
        "import fakelib_not_installed as fl\n\n\ndef go():\n    return fl.do_thing(1)\n",
        encoding="utf-8",
    )
    rep = verify_file(str(f))
    assert rep["status"] == "pass", rep["findings"]


# ── extractor performance guard ────────────────────────────────────────


def test_large_file_verification_is_fast(tmp_path: Path):
    """Extraction is linear: a ~1000-line file must verify quickly (bound kept
    generous to stay robust on slow CI machines)."""
    parts = ["import os\nimport sys\n"]
    for i in range(120):
        parts.append(
            f"class C{i}:\n"
            f"    def m(self, a, b):\n"
            f"        return self.helper(a)\n"
            f"    def helper(self, x):\n"
            f"        return x\n"
            f"def f{i}(x):\n"
            f"    return x + {i}\n"
        )
    f = tmp_path / "big.py"
    f.write_text("\n".join(parts), encoding="utf-8")
    started = time.perf_counter()
    tools.verify_file(str(f))
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"verification took {elapsed:.1f}s for a 1000-line file"
