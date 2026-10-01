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

REPO = Path(__file__).parent
ENV_BASE = {**os.environ, "PYTHONPATH": str(REPO), "PYTHONUNBUFFERED": "1"}


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


def main() -> int:
    # A realistic mini-project with a hallucination for the tool call.
    work = Path(tempfile.mkdtemp(prefix="hc-mcp-e2e-"))
    pkg = work / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "client.py").write_text(
        "class UserClient:\n    def refresh(self):\n        return 1\n\n"
        "    def refresh_access_token(self):\n        return 2\n"
    )
    broken = pkg / "login.py"
    broken.write_text(
        "from pkg.client import UserClient\n\n\ndef login(client: UserClient):\n"
        "    return client.refresh_token()\n"
    )

    print("=" * 70)
    print("A) DEFAULT MODE (no HONESTCODE_TOOLS) — expect only scan_file")
    print("=" * 70)
    resp = session(None, [{"name": "verify_file", "arguments": {"file_path": str(broken)}}])
    init = resp[0].get("result", {})
    print("serverInfo:", json.dumps(init.get("serverInfo", {})))
    print("protocolVersion:", init.get("protocolVersion"))
    tools = resp[1].get("result", {}).get("tools", [])
    print("tools exposed:", [t["name"] for t in tools])
    call = resp[2]
    if "error" in call:
        print("verify_file -> JSON-RPC error:", json.dumps(call["error"])[:200])
    else:
        text = call["result"]["content"][0]["text"]
        print("verify_file (unexpected, not whitelisted):", text[:120])

    print()
    print("=" * 70)
    print("B) FULL MODE (HONESTCODE_TOOLS=all) — call scan_file + verify_file")
    print("=" * 70)
    calls = [
        {"name": "scan_file", "arguments": {"file_path": str(broken)}},
        {"name": "verify_file", "arguments": {"file_path": str(broken)}},
        {"name": "search_code", "arguments": {"pattern": "refresh"}},
    ]
    resp = session({"HONESTCODE_TOOLS": "all"}, calls)
    tools = resp[1].get("result", {}).get("tools", [])
    print(f"tools exposed ({len(tools)}):", sorted(t["name"] for t in tools))
    for r in resp[2:]:
        res = r.get("result", r.get("error"))
        if isinstance(res, dict) and "content" in res:
            payload = json.loads(res["content"][0]["text"])
            summary = {
                k: payload.get(k)
                for k in ("status", "success", "count", "engine", "error")
                if k in payload
            }
            if "findings" in payload:
                summary["findings"] = [
                    {"kind": f["kind"], "symbol": f.get("symbol")} for f in payload["findings"]
                ]
            if "issues" in payload:
                summary["issues"] = [i.get("name") for i in payload["issues"]]
            print(" ->", json.dumps(summary)[:300])
        else:
            print(" -> error:", json.dumps(res)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
