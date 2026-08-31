"""Run the HonestCode invented-API demo end-to-end.

Usage:
    cd demos/invented-api
    python run.py

This script mirrors the exact agent loop from the README:

    Agent writes code → HonestCode verifies → evidence → agent fixes → pass
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# Make the development honestcode package importable without installing it.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from honestcode.mcp.tools import index_project, verify_file


def run() -> int:
    here = Path(__file__).parent.resolve()
    index_project(str(here), force_rebuild=True)

    print("=" * 60)
    print("Agent writes code with an invented API...")
    print("=" * 60)
    broken = here / "app" / "login_broken.py"
    report = verify_file(str(broken))
    print(report["text"])
    print()

    print("=" * 60)
    print("Agent fixes the code...")
    print("=" * 60)
    fixed = here / "app" / "login_fixed.py"
    report = verify_file(str(fixed))
    print(report["text"])
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(run())
