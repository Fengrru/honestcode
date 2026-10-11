"""False-positive regression tests for ``scan_file``.

A hallucination detector's most important property is that it stays quiet on
legitimate code. Each test here is a piece of perfectly valid Python that
has historically tripped naive "undefined call" detectors; ``scan_file``
must report **zero** issues on each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from honestcode.mcp import tools as tools_mod
from honestcode.mcp.tools import index_project, scan_file

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path):
    """A project that defines the symbols the legitimate snippets rely on."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "helpers.py").write_text(
        "def compute(x):\n    return x * 2\n\n"
        "class Client:\n    def fetch(self):\n        return 1\n",
        encoding="utf-8",
    )
    index_project(str(tmp_path), force_rebuild=True)
    return tmp_path


def _scan(tmp_path: Path, code: str) -> list[dict]:
    f = tmp_path / "snippet.py"
    f.write_text(code, encoding="utf-8")
    res = scan_file(str(f))
    assert res["success"], f"scan failed: {res.get('error')}"
    return res["issues"]


def test_no_false_positive_on_all_builtins(project: Path):
    """Every builtin (any/all/frozenset/id/hash/ord/chr/pow/...) must be known."""
    code = (
        "x = [1, 2, 3]\n"
        "print(any(x), all(x))\n"
        "print(frozenset(x), complex(1, 2))\n"
        "print(id(x), hash(x), ord('a'), chr(65))\n"
        "print(hex(255), oct(8), bin(5))\n"
        "print(pow(2, 10), divmod(7, 2))\n"
        "print(format(3.14, '.2f'))\n"
        "it = iter(x); print(next(it))\n"
        "print(repr(x), ascii(x), vars(), dir(x))\n"
        "print(bin(0b1010), oct(0o17), hex(0xff))\n"
        "print(StopIteration, RuntimeError, NotImplementedError)\n"
        "print(OSError, FileNotFoundError, PermissionError)\n"
        "print(Warning, DeprecationWarning, UserWarning)\n"
        "assert isinstance(x, list)\n"
        "assert issubclass(TypeError, Exception)\n"
        "assert hasattr(x, 'append')\n"
        "print(getattr(x, 'append', None))\n"
        "print(callable(print), callable(1))\n"
        "print(isinstance(3, int))\n"
        "print(type(x), type(1), type('a'))\n"
        "print(property, classmethod, staticmethod)\n"
        "print(memoryview(b'abc'), bytearray(b'x'), bytes(5))\n"
        "print(complex, bool, float, int, str, list, dict, set, tuple)\n"
        "print(slice(1, 5), object, enumerate(x), zip(x, x), map(print, x), filter(None, x))\n"
        "print(sorted(x), reversed(x), sum(x), min(x), max(x), abs(-3), round(3.14))\n"
        "print(len(x), range(3), input, open, print, breakpoint)  # noqa\n"
        "try:\n    pass\nexcept (KeyError, IndexError, AttributeError):\n    pass\n"
    )
    issues = _scan(project, code)
    assert issues == [], f"False positives: {issues}"


def test_no_false_positive_on_self_method(project: Path):
    """``self.helper()`` must not be reported as undefined."""
    code = (
        "class Service:\n"
        "    def __init__(self):\n        self.value = 0\n"
        "    def run(self):\n        return self._compute()\n"
        "    def _compute(self):\n        return self.value + 1\n"
    )
    issues = _scan(project, code)
    assert issues == [], f"False positives on self.method: {issues}"


def test_no_false_positive_on_from_import(project: Path):
    """``from pkg.helpers import compute; compute(5)`` must resolve."""
    code = (
        "from pkg.helpers import compute\n"
        "from pkg.helpers import Client as Cl\n"
        "compute(5)\n"
        "c = Cl()\n"
        "c.fetch()\n"
    )
    issues = _scan(project, code)
    assert issues == [], f"False positives on from-import: {issues}"


def test_no_false_positive_on_relative_import(project: Path):
    """Relative imports inside a package must resolve."""
    (project / "pkg" / "user.py").write_text(
        "from .helpers import compute, Client\ncompute(5)\nClient().fetch()\n",
        encoding="utf-8",
    )
    f = project / "pkg" / "user.py"
    res = scan_file(str(f))
    assert res["success"]
    assert res["issues"] == [], f"False positives on relative import: {res['issues']}"


def test_no_false_positive_on_import_as(project: Path):
    """``import numpy as np; np.array(...)`` — np is bound by the alias."""
    code = "import os\nimport os.path as op\nprint(os.getcwd())\nprint(op.join('a', 'b'))\n"
    issues = _scan(project, code)
    assert issues == [], f"False positives on import-as: {issues}"


def test_no_false_positive_on_walrus(project: Path):
    """``if (n := compute(5)):`` binds n, so n must not be flagged."""
    code = "from pkg.helpers import compute\nif (n := compute(5)):\n    print(n)\n"
    issues = _scan(project, code)
    assert issues == [], f"False positives on walrus: {issues}"


def test_no_false_positive_on_loop_variable(project: Path):
    """``for item in items: item.x`` — item is bound by the for-target."""
    code = "items = [1, 2, 3]\nfor item in items:\n    print(item)\nwhile items:\n    items.pop()\n"
    issues = _scan(project, code)
    assert issues == [], f"False positives on loop var: {issues}"


def test_no_false_positive_on_nested_function(project: Path):
    """A nested function calling its enclosing sibling must resolve."""
    code = (
        "def outer():\n"
        "    def inner():\n        return 1\n"
        "    return inner()\n"
        "def caller():\n    return outer()\n"
    )
    issues = _scan(project, code)
    assert issues == [], f"False positives on nested funcs: {issues}"


def test_no_false_positive_on_dataclass_style(project: Path):
    """Type-annotated assignments and dataclass-style code must not trip."""
    code = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Point:\n    x: int = 0\n    y: int = 0\n"
        "p = Point(x=1)\n"
        "print(p.x, p.y)\n"
    )
    issues = _scan(project, code)
    assert issues == [], f"False positives on dataclass: {issues}"


def test_no_false_positive_on_complex_call_target(project: Path):
    """``(a + b).foo()`` — call_name returns None, must be skipped, not flagged."""
    code = "a = 'x'; b = 'y'\nprint((a + b).upper())\n"
    issues = _scan(project, code)
    assert issues == [], f"False positives on complex call target: {issues}"


def test_undefined_call_still_detected(project: Path):
    """Sanity check: a truly undefined call must still be reported."""
    code = "totally_made_up_function_xyz()\n"
    issues = _scan(project, code)
    assert any(i["name"] == "totally_made_up_function_xyz" for i in issues)


# ── Auto-discovery without a project marker ───────────────────────────────
# With no marker the index falls back to the verified file's package root.
# Indexing only the file's immediate directory hid sibling packages and reported
# correct cross-package imports as undefined symbols — a false positive that
# sends the agent to "fix" working code.


def _no_marker_project(tmp_path: Path) -> Path:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "lib" / "http.py").write_text(
        "class Connection:\n    def execute(self, sql):\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _scan_in(tmp_path: Path, subdir: str, code: str) -> list[dict]:
    """Scan *code* from inside *subdir*, with no explicit index and no marker."""
    f = tmp_path / subdir / "snippet.py"
    f.write_text(code, encoding="utf-8")
    res = scan_file(str(f))
    assert res["success"], f"scan failed: {res.get('error')}"
    return res["issues"]


def _clear_index_state(monkeypatch) -> None:
    monkeypatch.setattr(tools_mod, "_project_index", None)
    monkeypatch.setattr(tools_mod, "_project_root", None)
    monkeypatch.setattr(tools_mod, "_index_by_root", {})


def test_cross_package_import_resolves_without_marker(tmp_path: Path, monkeypatch):
    """A sibling package stays importable when the project has no marker file."""
    _clear_index_state(monkeypatch)
    project = _no_marker_project(tmp_path)

    code = (
        "from lib.http import Connection as Conn\ndef f():\n    return Conn().execute('select 1')\n"
    )
    issues = _scan_in(project, "cases", code)
    assert issues == [], f"False positives without a marker: {issues}"


def test_invented_api_still_detected_without_marker(tmp_path: Path, monkeypatch):
    """Widening the scope must not silence detection inside it."""
    _clear_index_state(monkeypatch)
    project = _no_marker_project(tmp_path)

    code = "from lib.http import Connection\ndef f():\n    return Connection().missing_method()\n"
    issues = _scan_in(project, "cases", code)
    assert any(i["name"] == "missing_method" for i in issues), issues


def test_package_root_walks_out_of_packages(tmp_path: Path):
    """The import root is the first ancestor that is not itself a package."""
    pkg = tmp_path / "outer" / "inner"
    pkg.mkdir(parents=True)
    (tmp_path / "outer" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    assert tools_mod._package_root(pkg) == tmp_path

    # A directory that is not a package is already an import root.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert tools_mod._package_root(plain) == plain


def test_package_root_refuses_filesystem_root():
    """Never adopt a drive root as the project root."""
    from pathlib import Path as _Path

    anchor = _Path(_Path.cwd().anchor)
    assert tools_mod._package_root(anchor) is None


def test_undefined_import_still_detected_without_marker(tmp_path: Path, monkeypatch):
    """A name the sibling module really does not define is still reported."""
    _clear_index_state(monkeypatch)
    project = _no_marker_project(tmp_path)

    issues = _scan_in(project, "cases", "from lib.http import NoSuchThing\nNoSuchThing()\n")
    assert any(i["name"] == "NoSuchThing" for i in issues), issues
