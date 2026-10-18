"""Example: scan a file for undefined calls and validate a code snippet."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from honestcode.mcp.tools import index_project, scan_file, validate_types


def main(project_root: str) -> None:
    index_project(project_root)

    # Write a temporary file inside the project: scan_file grounds a file
    # against the project that contains it, so a file in the system temp
    # directory would make the example index an unrelated (and potentially
    # enormous or undecodable) tree instead of the requested project root.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8", dir=project_root
    ) as f:
        f.write("print(len([1, 2, 3]))\n")
        f.write("undefined_helper()\n")
        tmp_path = f.name

    try:
        print("scan_file:", scan_file(tmp_path))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    print("validate_types:", validate_types("for x in None:\n    pass\n"))


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    main(root)
