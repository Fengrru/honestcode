"""Tests for the repository-grounded verification layer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from honestcode.mcp import tools as tools_mod
from honestcode.mcp.tools import index_project, scan_file, verify_file
from honestcode.verify.evidence import CONFIDENCE_DETERMINISTIC, CONFIDENCE_HIGH

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tiny project with a client class and auth module."""
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "auth" / "client.py").write_text(
        "class UserClient:\n"
        "    def refresh(self):\n        return 'r'\n"
        "    def refresh_access_token(self):\n        return 'at'\n",
        encoding="utf-8",
    )
    index_project(str(tmp_path), force_rebuild=True)
    return tmp_path


def _finding(project: Path, code: str, file_name: str = "snippet.py") -> dict | None:
    f = project / file_name
    f.write_text(code, encoding="utf-8")
    res = verify_file(str(f))
    assert res["success"], res.get("error")
    findings = res["findings"]
    return findings[0] if findings else None


def test_invented_api_via_annotation(project: Path):
    """``client.refresh_token()`` is caught when the parameter is annotated."""
    code = (
        "from auth.client import UserClient\n"
        "def login(client: UserClient):\n"
        "    client.refresh_token()\n"
    )
    finding = _finding(project, code)
    assert finding is not None
    assert finding["kind"] == "invented_api"
    assert finding["symbol"] == "refresh_token"
    assert finding["owner"] == "UserClient"
    assert finding["confidence"] == CONFIDENCE_DETERMINISTIC
    assert finding["evidence"]["did_you_mean"] == "refresh_access_token"
    assert "refresh" in finding["evidence"]["available_methods"]
    assert "refresh_access_token" in finding["evidence"]["available_methods"]


def test_invented_api_via_constructor(project: Path):
    """``client.refresh_token()`` is caught when the variable is assigned from a constructor."""
    code = (
        "from auth.client import UserClient\n"
        "def login():\n"
        "    client = UserClient()\n"
        "    client.refresh_token()\n"
    )
    finding = _finding(project, code)
    assert finding is not None
    assert finding["kind"] == "invented_api"
    assert finding["symbol"] == "refresh_token"


def test_invented_api_on_self(project: Path):
    """``self.refresh_token()`` is caught inside a class that defines refresh()."""
    code = (
        "from auth.client import UserClient\n"
        "class AuthService:\n"
        "    def __init__(self):\n        self.client = UserClient()\n"
        "    def do_refresh(self):\n        self.client.refresh_token()\n"
    )
    finding = _finding(project, code)
    assert finding is not None
    assert finding["kind"] == "invented_api"


def test_legitimate_call_is_not_flagged(project: Path):
    """A real method call must produce a pass report."""
    code = (
        "from auth.client import UserClient\n"
        "def login(client: UserClient):\n"
        "    client.refresh_access_token()\n"
    )
    res = verify_file(str(project / "login.py"))
    (project / "login.py").write_text(code, encoding="utf-8")
    res = verify_file(str(project / "login.py"))
    assert res["status"] == "pass"
    assert res["findings"] == []
    assert "verification passed" in res["text"]


def test_self_method_not_flagged(project: Path):
    """``self._compute()`` on a class with no unknown bases must stay quiet."""
    code = (
        "class Service:\n"
        "    def run(self):\n        return self._compute()\n"
        "    def _compute(self):\n        return 1\n"
    )
    assert _finding(project, code) is None


def test_wrong_arity_on_known_method(project: Path):
    """Calling a known method with the wrong number of args is reported."""
    code = (
        "from auth.client import UserClient\n"
        "def login(client: UserClient):\n"
        "    client.refresh('extra')\n"
    )
    finding = _finding(project, code)
    assert finding is not None
    assert finding["kind"] == "wrong_call"
    assert "too many arguments" in finding["message"].lower()


def test_unresolved_base_stays_quiet(project: Path):
    """If a class inherits from an unknown base, do not invent false positives."""
    code = (
        "class Proxy(UnknownBase):\n"
        "    def go(self):\n        self.magic()\n"
    )
    assert _finding(project, code) is None


def test_high_confidence_typo_with_unresolved_base(project: Path):
    """A near-miss typo is still reported even when a base is unresolved."""
    code = (
        "class Proxy(UnknownBase):\n"
        "    def refresh(self):\n        return 'r'\n"
        "    def go(self):\n        self.refres()\n"
    )
    finding = _finding(project, code)
    assert finding is not None
    assert finding["kind"] == "invented_api"
    assert finding["confidence"] == CONFIDENCE_HIGH


def test_scan_file_legacy_shape(project: Path):
    """``scan_file`` keeps the pre-0.3 ``issues`` list for backwards compatibility."""
    f = project / "bad.py"
    f.write_text("nonexistent_func()\n", encoding="utf-8")
    res = scan_file(str(f))
    assert res["success"]
    assert res["status"] == "fail"
    assert any(i["name"] == "nonexistent_func" for i in res["issues"])
    assert any(f["symbol"] == "nonexistent_func" for f in res["findings"])


def test_verify_file_returns_text(project: Path):
    """``verify_file`` returns a human-readable ``text`` block."""
    f = project / "bad.py"
    f.write_text("nonexistent_func()\n", encoding="utf-8")
    res = verify_file(str(f))
    assert res["success"]
    assert res["status"] == "fail"
    assert "text" in res
    assert "verification failed" in res["text"]


def test_auto_index_discovers_project(tmp_path: Path, monkeypatch):
    """``scan_file`` auto-indexes a project when no index has been loaded."""
    # Clear any previously-loaded index.
    monkeypatch.setattr(tools_mod, "_project_index", None)
    monkeypatch.setattr(tools_mod, "_project_root", None)

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "client.py").write_text(
        "class UserClient:\n    def refresh(self): pass\n", encoding="utf-8"
    )
    f = tmp_path / "pkg" / "login.py"
    f.write_text(
        "from pkg.client import UserClient\n"
        "def login(client: UserClient):\n    client.refresh_token()\n",
        encoding="utf-8",
    )
    res = scan_file(str(f))
    assert res["success"]
    assert res["status"] == "fail"
    assert any(f["kind"] == "invented_api" for f in res["findings"])
