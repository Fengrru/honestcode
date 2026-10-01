"""Real-world false-positive benchmark.

Runs HonestCode over a vendored snapshot of a real-world project — a subset of
``psf/requests`` (see ``vendor/`` and its ``LICENSE``/``.upstream-commit``) —
and asserts it produces **zero** findings.

This is the guardrail the synthetic agent-accuracy tasks cannot provide: real
code is full of stdlib imports, keyword-argument call styles, ``@staticmethod``
members, classes inheriting stdlib ABCs, and compat re-export modules. Every
false-positive class found during the v0.4.0 real-world evaluation is
represented here, so a regression in any of them fails CI immediately.

Usage:
    python benchmarks/realworld_fp/run.py                # text summary
    python benchmarks/realworld_fp/run.py --format json  # machine-readable
    python benchmarks/realworld_fp/run.py --project /path/to/other/project
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from honestcode.mcp.tools import (  # noqa: E402
    index_project,
    load_project_deps,
    reset_dependency_state,
    verify_file,
)

VENDOR = Path(__file__).parent / "vendor"


def run(project: Path) -> dict:
    """Verify every Python file under *project*; return the aggregated result."""
    reset_dependency_state()
    index_project(str(project), force_rebuild=True)
    # Declared-only: the vendored project's third-party dependencies are not
    # necessarily installed, and importing them is not needed to prove that
    # valid code stays silent. Imports from declared packages are trusted and
    # member checks stay silent for surfaces we never enumerated.
    load_project_deps(str(project), import_packages=False)

    files = sorted(p for p in project.rglob("*.py") if "__pycache__" not in p.parts)
    findings: list[dict] = []
    started = time.perf_counter()
    for p in files:
        report = verify_file(str(p))
        for f in report.get("findings", []):
            findings.append({"file": str(p.relative_to(project)), **f})
    elapsed = time.perf_counter() - started

    return {
        "project": str(project),
        "files": len(files),
        "findings": findings,
        "count": len(findings),
        "elapsed_seconds": round(elapsed, 2),
        "ms_per_file": round(elapsed / len(files) * 1000, 1) if files else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-world false-positive benchmark for HonestCode."
    )
    parser.add_argument(
        "--project",
        default=str(VENDOR),
        help="Project directory to scan (default: the vendored requests snapshot).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format.",
    )
    args = parser.parse_args()

    project = Path(args.project)
    if not project.is_dir():
        print(f"Project not found: {project}", file=sys.stderr)
        return 1

    result = run(project)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        print("# HonestCode real-world false-positive benchmark\n")
        print(f"project: {result['project']}")
        print(
            f"files: {result['files']}  findings: {result['count']}  "
            f"elapsed: {result['elapsed_seconds']}s "
            f"({result['ms_per_file']} ms/file)"
        )
        for f in result["findings"]:
            print(f"  [{f['kind']}/{f['confidence']}] {f['file']}:{f['line']} {f['message']}")
        if result["count"] == 0:
            print("\nPASS: no false positives on real-world code.")
        else:
            print(f"\nFAIL: {result['count']} finding(s) on code that should be clean.")

    # Any finding on this corpus is a false positive: block the build.
    return 2 if result["count"] else 0


if __name__ == "__main__":
    sys.exit(main())
