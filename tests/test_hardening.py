"""Regression tests for the hardening pass.

Each test pins one bug that shipped in 0.4.0 and was fixed afterwards. They are
kept together so the reason a test exists is obvious without reading git
history; the older suites stay focused on their own area.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from honestcode.guards import (
    EXECUTING,
    FAILED,
    EffectClass,
    EffectStore,
    SideEffectGuard,
    durable_scope,
)
from honestcode.honest import project_binding as binding
from honestcode.mcp import tools
from honestcode.sandbox import SandboxExecutor
from honestcode.structure import multi_lang
from honestcode.structure.utils import iter_python_files, prune_cache_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_global_state(monkeypatch):
    """Every test starts with no project and no auto-declared dependencies."""
    monkeypatch.setattr(tools, "_project_index", None)
    monkeypatch.setattr(tools, "_project_root", None)
    monkeypatch.setattr(tools, "_index_by_root", {})
    monkeypatch.setattr(tools, "_graph_mem_cache", {})
    monkeypatch.setattr(tools, "_deps_stamp_by_root", {})
    tools.reset_dependency_state()
    yield
    tools.reset_dependency_state()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── source freshness ────────────────────────────────────────────────────────


def test_edited_definition_file_is_re_read(tmp_path: Path):
    """The surface cache must not serve bytes from before the agent's edit."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    base = _write(
        tmp_path / "pkg" / "a.py",
        "class A:\n    def existing(self):\n        return 1\n",
    )
    use = _write(
        tmp_path / "use.py",
        "from pkg.a import A\n\ndef f(a: A):\n    return a.missing_method()\n",
    )
    tools.index_project(str(tmp_path), force_rebuild=True)
    assert tools.verify_file(str(use))["status"] == "fail"

    time.sleep(0.02)  # make the edit visible even on coarse mtime filesystems
    base.write_text(
        "class A:\n    def existing(self):\n        return 1\n"
        "    def missing_method(self):\n        return 2\n",
        encoding="utf-8",
    )
    report = tools.verify_file(str(use))
    assert report["status"] == "pass", report["findings"]


# ── encodings and traversal ─────────────────────────────────────────────────


def test_non_utf8_project_file_does_not_break_scan(tmp_path: Path):
    """A legal PEP 263 file must not abort the index for every other file."""
    _write(tmp_path / "good.py", "def hello():\n    return 1\n")
    (tmp_path / "latin.py").write_bytes(
        b"# -*- coding: latin-1 -*-\ndef caf\xe9():\n    return 1\n"
    )
    report = tools.scan_file(str(tmp_path / "good.py"))
    assert report["success"] is True
    assert report["status"] == "pass"

    latin = tools.scan_file(str(tmp_path / "latin.py"))
    assert latin["success"] is True


def test_cache_pruning_removes_only_old_entries(tmp_path: Path):
    old = _write(tmp_path / "old.json", "{}")
    fresh = _write(tmp_path / "fresh.json", "{}")
    stale_time = time.time() - 40 * 86400
    os.utime(old, (stale_time, stale_time))

    prune_cache_files(tmp_path, ("*.json",), max_age_days=30)
    assert not old.exists()
    assert fresh.exists()


def test_virtual_environments_are_not_indexed(tmp_path: Path):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path / "app.py", "def real():\n    return 1\n")
    dep = tmp_path / ".venv" / "Lib" / "site-packages" / "fakedep"
    _write(dep / "__init__.py", "def dep_helper():\n    return 1\n")

    names = [p.relative_to(tmp_path).as_posix() for p in iter_python_files(tmp_path)]
    assert not any(name.startswith(".venv/") for name in names)

    res = tools.index_project(str(tmp_path), force_rebuild=True)
    assert res["symbols_indexed"] >= 1
    assert not any(".venv." in key for key in tools._project_index.symbols)


# ── non-Python verification contract ────────────────────────────────────────


def test_verify_file_on_non_python_uses_the_evidence_protocol(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(multi_lang, "detect_language", lambda _p: "javascript")
    monkeypatch.setattr(
        multi_lang,
        "extract_symbols",
        lambda _p: [{"name": "run", "line": 1, "kind": "function"}],
    )
    js = _write(tmp_path / "app.js", "function run() { return missing(); }\n")

    report = tools.verify_file(str(js))
    assert report["success"] is True
    assert report["status"] == "fail"
    assert report["findings"][0]["kind"] == "undefined_symbol"
    assert report["findings"][0]["symbol"] == "missing"
    assert report["findings"][0]["confidence"] == "low"
    assert "verification failed" in report["text"]
    assert report["language"] == "javascript"


# ── dependency declaration ──────────────────────────────────────────────────


def test_requirements_are_declared_automatically(tmp_path: Path):
    """A dependency that is not installed must not make its import a finding."""
    _write(tmp_path / "requirements.txt", "totally-absent-pkg-xyz\n")
    app = _write(
        tmp_path / "app.py",
        "from totally_absent_pkg_xyz import thing\n\ndef use():\n    return thing()\n",
    )
    report = tools.verify_file(str(app))
    assert report["status"] == "pass", report["findings"]


def test_pyproject_optional_dependencies_are_declared(tmp_path: Path):
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "demo"\n\n'
        "[project.optional-dependencies]\n"
        'agent = ["pydantic-ai-slim>=2.55"]\n',
    )
    app = _write(
        tmp_path / "app.py",
        "from pydantic_ai import Agent\n\ndef build():\n    return Agent(None)\n",
    )
    report = tools.verify_file(str(app))
    assert report["status"] == "pass", report["findings"]


# ── structural checks ───────────────────────────────────────────────────────


def test_validate_types_accepts_valid_builtin_calls():
    valid = (
        "str(b'x', 'utf-8')\n"
        "str(object=5)\n"
        "max([1, 2], default=0)\n"
        "min([1, 2], key=abs)\n"
        "sorted([2, 1], key=abs)\n"
    )
    assert tools.validate_types(valid)["issues"] == []


def test_validate_types_still_rejects_sorted_positional_key():
    issues = tools.validate_types("sorted([2, 1], None)\n")["issues"]
    assert any(i["type"] == "arg_count" for i in issues)


def test_module_level_method_injection_is_a_member(tmp_path: Path):
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "pkg" / "client.py",
        "class Client:\n    def real(self):\n        return 1\n\n"
        "def extra(self):\n    return 2\n\n"
        "Client.patched = extra\n",
    )
    use = _write(
        tmp_path / "use.py",
        "from pkg.client import Client\n\ndef f(c: Client):\n    return c.patched()\n",
    )
    tools.index_project(str(tmp_path), force_rebuild=True)
    report = tools.verify_file(str(use))
    assert report["status"] == "pass", report["findings"]


def test_check_symbol_honours_file_path(tmp_path: Path):
    a = _write(tmp_path / "pkg" / "a.py", "def helper():\n    return 1\n")
    b = _write(tmp_path / "pkg" / "b.py", "def helper():\n    return 2\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    tools.index_project(str(tmp_path), force_rebuild=True)

    assert tools.check_symbol("helper", file_path=str(a))["defined"] is True
    assert tools.check_symbol("helper", file_path=str(b))["defined"] is True
    assert tools.check_symbol("pkg.a.helper", file_path=str(b))["defined"] is False


# ── CLI state across processes ──────────────────────────────────────────────


def test_cli_query_commands_resolve_the_project_themselves(tmp_path: Path, capsys):
    from honestcode.cli import main as cli_main

    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "core.py", "def helper():\n    return 1\n")

    assert cli_main(["stats", "--root", str(tmp_path)]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["success"] is True

    # Simulate the next process: the in-memory index from `stats` is gone.
    tools._project_index = None
    tools._project_root = None
    assert cli_main(["check-symbol", "pkg.core.helper", "--root", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["defined"] is True


# ── MCP config installer ────────────────────────────────────────────────────


def test_install_mcp_config_refuses_to_overwrite_invalid_json(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    claude = home / ".claude.json"
    original = '// hand-edited config\n{"theme": "dark"}\n'
    claude.write_text(original, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    result = binding.install_mcp_config(path=str(project), client="claude")
    assert result["success"] is False
    assert result["errors"]
    assert claude.read_text(encoding="utf-8") == original


def test_install_mcp_config_refuses_non_object_servers(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    claude = home / ".claude.json"
    original = '{"mcpServers": []}'
    claude.write_text(original, encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    result = binding.install_mcp_config(path=str(project), client="claude")
    assert result["success"] is False
    assert claude.read_text(encoding="utf-8") == original


def test_install_mcp_config_preserves_unrelated_settings(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    claude = home / ".claude.json"
    claude.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    result = binding.install_mcp_config(path=str(project), client="claude")
    assert result["success"] is True
    payload = json.loads(claude.read_text(encoding="utf-8"))
    assert payload["theme"] == "dark"
    assert "honestcode" in payload["mcpServers"]


# ── git-backed tools ────────────────────────────────────────────────────────


def _git_project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    sub = repo / "sub"
    _write(sub / "app" / "__init__.py", "")
    _write(
        sub / "app" / "core.py",
        "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
    )
    _write(
        sub / "test_app.py",
        "from app.core import main\n\ndef test_main():\n    assert main() == 1\n",
    )
    _write(sub / "pyproject.toml", "[project]\nname='sub'\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
    return sub


def test_affected_files_works_when_project_root_is_a_repo_subdirectory(tmp_path: Path):
    sub = _git_project(tmp_path)
    tools.index_project(str(sub), force_rebuild=True)
    _write(
        sub / "app" / "core.py",
        "def helper():\n    return 2\n\ndef main():\n    return helper()\n",
    )
    res = tools.affected_files(base="HEAD")
    assert res["success"] is True
    assert res["changed"] == ["app/core.py"]
    assert any("test_app.py" in path for path in res["affected_tests"])


def test_affected_files_rejects_option_like_refs(tmp_path: Path):
    sub = _git_project(tmp_path)
    tools.index_project(str(sub), force_rebuild=True)
    out = tmp_path / "written-by-git.txt"
    res = tools.affected_files(base=f"--output={out.as_posix()}")
    assert res["success"] is False
    assert "invalid git ref" in res["error"]
    assert not out.exists()


# ── guards: atomic retry and scoped recovery ────────────────────────────────


class _RacingStore(EffectStore):
    """Pause both racers right after they read the same FAILED record."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.barrier = threading.Barrier(2)
        self.armed = False

    def find_by_key(self, session_id: str, idempotency_key: str):
        record = super().find_by_key(session_id, idempotency_key)
        if self.armed and record is not None and record.status == FAILED:
            with suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=5)
        return record


def test_concurrent_retry_of_a_failed_effect_runs_once(tmp_path: Path):
    store = _RacingStore(str(tmp_path / "effects.db"))
    guard = SideEffectGuard(session_id="s1", store=store, approve_irreversible=True)

    def boom():
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        guard.run("edit_file", target="a.py", payload={"v": 1}, execute=boom)

    calls: list[int] = []
    lock = threading.Lock()

    def execute():
        with lock:
            calls.append(1)
        return "ok"

    store.armed = True
    outcomes: list[object] = []
    errors: list[Exception] = []

    def attempt() -> None:
        try:
            outcomes.append(
                guard.run("edit_file", target="a.py", payload={"v": 1}, execute=execute)
            )
        except Exception as exc:  # noqa: BLE001 - the loser may be refused
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    guard.close()

    assert len(calls) == 1, f"the effect ran {len(calls)} times"
    assert sum(1 for outcome in outcomes if outcome.executed) <= 1
    assert len(outcomes) + len(errors) == 2


def test_scoped_effects_are_visible_to_recovery(tmp_path: Path):
    db = str(tmp_path / "effects.db")
    scope = durable_scope("order-1", "charge", "cus_1")

    first = SideEffectGuard(session_id="session-1", store_path=db, approve_irreversible=True)
    record, _ = first._claim(
        action_type="charge",
        target="cus_1",
        payload={"cents": 100},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
        scope=scope,
    )
    first.store.set_status(record.effect_id, EXECUTING)
    first.close()

    second = SideEffectGuard(session_id="session-2", store_path=db)
    assert second.recover() == []
    decisions = second.recover(scope=scope)
    assert [decision.action for decision in decisions] == ["query_external"]
    assert second.effects(scope=scope)[0].status == EXECUTING
    assert second.summary(scope=scope) == {"executing": 1}
    second.close()


def test_effect_store_migrates_an_old_schema(tmp_path: Path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE side_effects (
            effect_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, action_type TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', effect_class TEXT NOT NULL,
            payload_hash TEXT NOT NULL DEFAULT '',
            preconditions TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL, response_hash TEXT, compensation TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            committed_at INTEGER, UNIQUE (session_id, idempotency_key)
        );
        """
    )
    conn.close()

    store = EffectStore(db)
    record = store.insert(
        effect_id="se_1",
        session_id="scope-ns:x",
        idempotency_key="k",
        action_type="charge",
        target="cus_1",
        effect_class=EffectClass.IRREVERSIBLE,
        payload_hash="h",
        preconditions=(),
        scope="order-1",
    )
    store.close()
    assert record is not None
    assert record.scope == "order-1"


# ── server entry points and transport safety ────────────────────────────────


def test_run_mcp_server_script_registers_tools(monkeypatch):
    import honestcode.mcp.server as server_mod

    spec = importlib.util.spec_from_file_location(
        "run_mcp_server_script", ROOT / "scripts" / "run_mcp_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setattr(server_mod.mcp, "run", lambda **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["run_mcp_server.py"])
    monkeypatch.delenv("HONESTCODE_TOOLS", raising=False)
    module.main()

    manager = getattr(server_mod.mcp, "_tool_manager", None)
    registered = set(getattr(manager, "_tools", {}))
    assert {"scan_file", "verify_file"} <= registered


def test_non_loopback_sse_requires_an_explicit_opt_in(monkeypatch):
    from honestcode.mcp import server as server_mod

    monkeypatch.delenv("HONESTCODE_ALLOW_NETWORK", raising=False)
    monkeypatch.setattr(server_mod, "register_tools", lambda: None)
    with pytest.raises(SystemExit) as info:
        server_mod.main(["--transport", "sse", "--host", "0.0.0.0"])
    assert info.value.code == 2


# ── multi-language symbol extraction ────────────────────────────────────────


@pytest.mark.parametrize(
    ("ext", "source", "expected"),
    [
        (
            "js",
            "function outer() { function inner() { return 1; } return inner(); }",
            {"outer", "inner"},
        ),
        (
            "ts",
            "export class Foo { bar() { return 1; } }\nexport interface Shape {}\n",
            {"Foo", "bar", "Shape"},
        ),
        ("tsx", "export function App() { return <div/>; }\n", {"App"}),
        (
            "go",
            "package main\n\ntype Server struct{}\n\nfunc (s Server) Run() {}\n\nfunc main() {}\n",
            {"Server", "Run", "main"},
        ),
        ("rs", "struct Server;\n\nenum Mode { A }\n\nfn main() {}\n", {"Server", "Mode", "main"}),
        ("java", "class Server { void run() {} }\n", {"Server", "run"}),
    ],
)
def test_multi_language_symbols_are_extracted(tmp_path: Path, ext, source, expected):
    pytest.importorskip("tree_sitter", reason="tree-sitter extras not installed")
    path = _write(tmp_path / f"sample.{ext}", source)
    try:
        symbols = multi_lang.extract_symbols(path)
    except multi_lang.TreeSitterUnavailable as exc:
        pytest.skip(str(exc))
    names = {symbol["name"] for symbol in symbols}
    assert expected <= names, f"missing {expected - names} in {sorted(names)}"


# ── Windows sandbox limits ──────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object API only")
def test_windows_job_object_is_applied():
    """The wrong JOB_OBJECT_LIMIT constant made SetInformationJobObject fail.

    Before the fix ``_apply_windows_job`` returned ``None`` on every Windows
    machine, so memory/CPU/kill-on-close limits were silently absent.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    executor = SandboxExecutor()
    handle = None
    try:
        handle = executor._apply_windows_job(proc)
        assert handle, "Job Object assignment failed; limits are not applied"
    finally:
        executor._release_windows_job(handle)
        proc.kill()
        proc.wait()
