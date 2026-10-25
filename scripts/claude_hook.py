"""Claude Code PostToolUse hook: verify every .py file the agent writes.

This removes the "relies on the agent remembering to call verify_file"
dependency: Claude Code runs this script after every Write/Edit tool call,
and the verification verdict is fed straight back into the conversation.

Wiring (in `~/.claude/settings.json` or the project's
`.claude/settings.json`):

    {
      "hooks": {
        "PostToolUse": [
          {
            "matcher": "Write|Edit|NotebookEdit",
            "hooks": [
              {
                "type": "command",
                "command": "python /absolute/path/to/honestcode/scripts/claude_hook.py"
              }
            ]
          }
        ]
      }
    }

Contract: Claude Code passes one JSON object on stdin describing the tool
call. Exit code 0 means "nothing to say"; exit code 2 blocks the edit and
feeds *stderr* back to the model, which is exactly the shape a verification
finding needs. Any other failure mode exits 0 — a broken hook must never
turn into a wall that blocks every edit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["hook_exit_code", "main"]


def hook_exit_code(payload: dict) -> int:
    """Map one hook payload to a Claude Code exit code."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("filePath") or ""
    file_path = str(file_path)
    if not file_path or not file_path.endswith(".py"):
        return 0
    p = Path(file_path)
    if not p.is_file():
        return 0  # a deletion or a not-yet-written path: nothing to verify

    from honestcode.mcp.tools import verify_file
    from honestcode.verify.evidence import render_text

    try:
        report = verify_file(file_path)
    except Exception:  # noqa: BLE001 - a broken hook must not block edits
        return 0

    if not report.get("findings"):
        return 0

    # Exit code 2: Claude Code shows stderr to the model and blocks the edit,
    # so the agent sees the evidence in the same turn it wrote the code.
    sys.stderr.write(render_text(report))
    sys.stderr.write("\n")
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # unparseable input: stay silent rather than block
    if not isinstance(payload, dict):
        return 0
    return hook_exit_code(payload)


if __name__ == "__main__":
    sys.exit(main())
