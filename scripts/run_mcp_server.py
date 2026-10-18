"""
Launch the HonestCode MCP server.

Usage:
    python scripts/run_mcp_server.py
    python scripts/run_mcp_server.py --transport stdio   # default

The server communicates over stdio by default, which is what MCP clients
(Cursor, Claude Desktop, VS Code) expect.

This is a thin launcher around ``honestcode.mcp.server``: delegating to its
``main`` keeps tool registration and the non-loopback SSE guard in one place.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the package root is importable when run directly
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from honestcode.mcp.server import main as server_main  # noqa: E402


def main() -> None:
    server_main(sys.argv[1:])


if __name__ == "__main__":
    main()
