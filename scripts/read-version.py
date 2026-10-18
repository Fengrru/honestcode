"""Print the project version from honestcode/__init__.py.

``pyproject.toml`` declares the version dynamically from that attribute, so
there is exactly one place to bump.

Usage: python scripts/read-version.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from honestcode import __version__

    print(__version__)


if __name__ == "__main__":
    main()
