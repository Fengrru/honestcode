"""Tests for the Claude Code PostToolUse hook (scripts/claude_hook.py)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path  # noqa: TC003

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "claude_hook.py"
# The hook script runs with sys.path[0] = scripts/; hand it the repo root so
# the package imports the same way it does in an installed environment.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _run_hook(payload: dict | str) -> subprocess.CompletedProcess[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            p for p in (_REPO_ROOT, os.environ.get("PYTHONPATH", "")) if p
        ),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=text,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "lib.py").write_text(
        "class UserClient:\n    def refresh(self):\n        return 1\n",
        encoding="utf-8",
    )
    return tmp_path


def test_hook_blocks_an_invented_api(project: Path):
    app = project / "app.py"
    app.write_text(
        "from lib import UserClient\n\n"
        "def login(client: UserClient):\n"
        "    client.refresh_token()\n",
        encoding="utf-8",
    )
    proc = _run_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(app), "content": "..."}}
    )
    assert proc.returncode == 2
    assert "refresh_token" in proc.stderr
    assert "did you mean" in proc.stderr


def test_hook_passes_clean_files_silently(project: Path):
    app = project / "app.py"
    app.write_text(
        "from lib import UserClient\n\n"
        "def login(client: UserClient):\n"
        "    return client.refresh()\n",
        encoding="utf-8",
    )
    proc = _run_hook({"tool_input": {"file_path": str(app)}})
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_hook_ignores_non_python_and_missing_files(project: Path):
    """A broken hook must never block an unrelated edit."""
    plain = project / "notes.md"
    plain.write_text("hello", encoding="utf-8")
    for payload in (
        {"tool_input": {"file_path": str(plain)}},
        {"tool_input": {"file_path": str(project / "nope.py")}},
        {"tool_input": {}},
        {},
        "not json at all",
    ):
        assert _run_hook(payload).returncode == 0
