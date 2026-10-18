"""End-to-end MCP client test: spawn the real server over stdio and talk to it.

Sends actual JSON-RPC MCP messages (initialize -> tools/list -> tools/call)
the way Claude Code / Cursor would, and prints the raw responses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The test controls tool visibility per session; an inherited value would make
# the "default mode" section silently run in a different mode.
_CONTROLLED_ENV = {"HONESTCODE_TOOLS", "HONESTCODE_AUTO_INDEX", "HONESTCODE_AUTO_DEPS"}
ENV_BASE = {
    **{k: v for k, v in os.environ.items() if k not in _CONTROLLED_ENV},
    "PYTHONPATH": str(REPO),
    "PYTHONUNBUFFERED": "1",
}


def _spawn(extra_env: dict | None = None) -> subprocess.Popen:
    env = dict(ENV_BASE)
    env.update(extra_env or {})
    return subprocess.Popen(
        [sys.executable, "-m", "honestcode.mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _rpc(proc: subprocess.Popen, msg: dict, timeout_lines: int = 1) -> dict:
    assert proc.stdin and proc.stdout
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"no response; stderr:\n{err[:2000]}")
    return json.loads(line)


def _drain_notifications(proc: subprocess.Popen, want_id: int) -> dict:
    """Read until we get the response with the matching id (skip notifications)."""
    for _ in range(50):
        assert proc.stdout
        line = proc.stdout.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg
    raise RuntimeError("no matching response id")


def session(extra_env: dict | None, calls: list[dict]) -> list[dict]:
    proc = _spawn(extra_env)
    out: list[dict] = []
    try:
        # 1. initialize
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hc-e2e-test", "version": "0.1"},
            },
        }
        out.append(_rpc(proc, init))
        # 2. initialized notification (no id, no response)
        assert proc.stdin
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()
        # 3. tools/list
        out.append(_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        # 4. tool calls
        for i, call in enumerate(calls, start=10):
            out.append(
                _rpc(proc, {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": call})
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return out


def _payload(response: dict) -> dict:
    """Extract the JSON payload from a tools/call result."""
    result = response.get("result")
    if not isinstance(result, dict) or not result.get("content"):
        raise RuntimeError(f"tool call failed: {json.dumps(response)[:500]}")
    return json.loads(result["content"][0]["text"])


def main() -> int:
    failures: list[str] = []
    # A realistic mini-project with a hallucination for the tool call.
    with tempfile.TemporaryDirectory(prefix="hc-mcp-e2e-") as td:
        work = Path(td)
        pkg = work / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "client.py").write_text(
            "class UserClient:\n    def refresh(self):\n        return 1\n\n"
            "    def refresh_access_token(self):\n        return 2\n",
            encoding="utf-8",
        )
        broken = pkg / "login.py"
        broken.write_text(
            "from pkg.client import UserClient\n\n\ndef login(client: UserClient):\n"
            "    return client.refresh_token()\n",
            encoding="utf-8",
        )

        print("=" * 70)
        print("A) DEFAULT MODE (no HONESTCODE_TOOLS) — expect scan_file + verify_file")
        print("=" * 70)
        resp = session(None, [{"name": "verify_file", "arguments": {"file_path": str(broken)}}])
        init = resp[0].get("result", {})
        print("serverInfo:", json.dumps(init.get("serverInfo", {})))
        print("protocolVersion:", init.get("protocolVersion"))
        tool_names = {t["name"] for t in resp[1].get("result", {}).get("tools", [])}
        print("tools exposed:", sorted(tool_names))
        if tool_names != {"scan_file", "verify_file"}:
            failures.append(f"default tool set is {sorted(tool_names)}")
        payload = _payload(resp[2])
        print(
            "verify_file ->",
            payload.get("status"),
            [(f["kind"], f.get("symbol")) for f in payload.get("findings", [])],
        )
        if payload.get("status") != "fail" or not any(
            f.get("symbol") == "refresh_token" for f in payload.get("findings", [])
        ):
            failures.append("verify_file did not catch refresh_token in default mode")

        print()
        print("=" * 70)
        print("B) FULL MODE (HONESTCODE_TOOLS=all) — scan_file + verify_file + search_code")
        print("=" * 70)
        calls = [
            {"name": "verify_file", "arguments": {"file_path": str(broken)}},
            {"name": "scan_file", "arguments": {"file_path": str(broken)}},
            {"name": "search_code", "arguments": {"pattern": "refresh"}},
        ]
        resp = session({"HONESTCODE_TOOLS": "all"}, calls)
        tool_names = {t["name"] for t in resp[1].get("result", {}).get("tools", [])}
        print(f"tools exposed ({len(tool_names)}):", sorted(tool_names))
        if not {"scan_file", "verify_file", "search_code"} <= tool_names:
            failures.append("full mode is missing a core tool")
        for response in resp[2:]:
            payload = _payload(response)
            summary = {
                key: payload.get(key)
                for key in ("status", "success", "count", "engine", "error")
                if key in payload
            }
            print(" ->", json.dumps(summary)[:300])
        if _payload(resp[2]).get("status") != "fail":
            failures.append("full-mode verify_file did not fail the broken file")
        if _payload(resp[4]).get("count", 0) < 1:
            failures.append("search_code returned no matches")

    if failures:
        print("\nE2E FAIL:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nE2E PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
