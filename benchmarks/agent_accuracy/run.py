"""Deterministic accuracy benchmark for HonestCode as an agent verification layer.

The benchmark treats each task as a tiny agent episode:

1. The agent writes a file that contains a known hallucination.
2. HonestCode verifies it; we expect a finding.
3. The agent fixes the file.
4. HonestCode verifies it again; we expect silence.

This gives precision / recall / false-positive rate numbers that are cheap to
reproduce and do not depend on an LLM being available.

Usage:
    cd benchmarks/agent_accuracy
    python run.py                              # text summary
    python run.py --format markdown            # table for README
    python run.py --format json                # machine-readable
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from honestcode.mcp.tools import index_project, verify_file  # noqa: E402


class Task:
    """A single benchmark task loaded from disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.meta: dict[str, Any] = json.loads((path / "task.json").read_text(encoding="utf-8"))
        self.context_dir = path / "context"
        self.broken_source = (path / "broken" / "app.py").read_text(encoding="utf-8")
        self.fixed_source = (path / "fixed" / "app.py").read_text(encoding="utf-8")

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def description(self) -> str:
        return self.meta["description"]

    @property
    def target_file(self) -> str:
        return self.meta["target_file"]

    @property
    def expected_kind(self) -> str:
        return self.meta["expected_issue_kind"]

    @property
    def expected_symbol(self) -> str:
        return self.meta["expected_symbol"]


def load_tasks(dataset_dir: Path) -> list[Task]:
    """Load every task directory under *dataset_dir*."""
    tasks: list[Task] = []
    for entry in sorted(dataset_dir.iterdir()):
        if entry.is_dir() and (entry / "task.json").exists():
            tasks.append(Task(entry))
    return tasks


def _matches_expected(finding: dict[str, Any], task: Task) -> bool:
    """Return True if a finding matches the task's expected issue."""
    if finding.get("kind") != task.expected_kind:
        return False
    symbol = finding.get("symbol", "")
    message = finding.get("message", "")
    needle = task.expected_symbol
    return needle in symbol or needle in message


def _run_once(task: Task, source: str, root: Path) -> dict[str, Any]:
    """Write *source* into the task temp root and run verify_file."""
    target = root / task.target_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    # Force a clean index for this synthetic project root.
    index_project(str(root), force_rebuild=True)
    started = time.perf_counter()
    report = verify_file(str(target))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {"report": report, "elapsed_ms": elapsed_ms}


def evaluate_task(task: Task) -> dict[str, Any]:
    """Evaluate one task and return structured results."""
    with tempfile.TemporaryDirectory(prefix="hc-bench-") as td:
        root = Path(td)
        if task.context_dir.exists():
            shutil.copytree(task.context_dir, root, dirs_exist_ok=True)

        broken = _run_once(task, task.broken_source, root)
        fixed = _run_once(task, task.fixed_source, root)

        broken_findings = broken["report"].get("findings", [])
        broken_match = any(_matches_expected(f, task) for f in broken_findings)

        fixed_findings = fixed["report"].get("findings", [])
        fixed_status = fixed["report"].get("status", "unknown")
        fixed_clean = fixed_status == "pass" and not fixed_findings

        return {
            "name": task.name,
            "description": task.description,
            "expected_kind": task.expected_kind,
            "expected_symbol": task.expected_symbol,
            "broken": {
                "status": broken["report"].get("status"),
                "findings_count": len(broken_findings),
                "detected": broken_match,
                "findings": broken_findings,
                "elapsed_ms": round(broken["elapsed_ms"], 2),
            },
            "fixed": {
                "status": fixed_status,
                "findings_count": len(fixed_findings),
                "clean": fixed_clean,
                "findings": fixed_findings,
                "elapsed_ms": round(fixed["elapsed_ms"], 2),
            },
        }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute precision, recall, F1, and false-positive rate."""
    tp = sum(1 for r in results if r["broken"]["detected"])
    fn = sum(1 for r in results if not r["broken"]["detected"])
    tn = sum(1 for r in results if r["fixed"]["clean"])
    fp = sum(1 for r in results if not r["fixed"]["clean"])

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0

    elapsed = [r["broken"]["elapsed_ms"] + r["fixed"]["elapsed_ms"] for r in results]

    return {
        "tasks": len(results),
        "true_positives": tp,
        "false_negatives": fn,
        "true_negatives": tn,
        "false_positives": fp,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "false_positive_rate": round(fpr, 3),
        "median_verify_ms": round(statistics.median(elapsed), 2) if elapsed else 0.0,
    }


def render_text(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines = ["# HonestCode agent-accuracy benchmark\n"]
    for r in results:
        lines.append(f"## {r['name']}")
        lines.append(f"{r['description']}")
        b = r["broken"]
        f = r["fixed"]
        lines.append(
            f"- broken: detected={b['detected']}, status={b['status']}, "
            f"findings={b['findings_count']}, elapsed={b['elapsed_ms']}ms"
        )
        lines.append(
            f"- fixed: clean={f['clean']}, status={f['status']}, "
            f"findings={f['findings_count']}, elapsed={f['elapsed_ms']}ms"
        )
        if not b["detected"]:
            lines.append("  ! expected issue was NOT detected")
        if not f["clean"]:
            lines.append("  ! fixed version still reports findings")
        lines.append("")

    lines.append("# Summary")
    lines.append(f"tasks: {metrics['tasks']}")
    lines.append(f"true positives:  {metrics['true_positives']}")
    lines.append(f"false negatives: {metrics['false_negatives']}")
    lines.append(f"true negatives:  {metrics['true_negatives']}")
    lines.append(f"false positives: {metrics['false_positives']}")
    lines.append(f"precision: {metrics['precision']}")
    lines.append(f"recall:    {metrics['recall']}")
    lines.append(f"F1:        {metrics['f1']}")
    lines.append(f"false positive rate: {metrics['false_positive_rate']}")
    lines.append(f"median verify time:  {metrics['median_verify_ms']}ms")
    return "\n".join(lines)


def render_markdown(results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines = [
        "| task | expected issue | broken detected | fixed clean | broken ms | fixed ms |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        b = r["broken"]
        f = r["fixed"]
        lines.append(
            f"| {r['name']} | {r['expected_kind']} (`{r['expected_symbol']}`) | "
            f"{'yes' if b['detected'] else 'no'} | {'yes' if f['clean'] else 'no'} | "
            f"{b['elapsed_ms']} | {f['elapsed_ms']} |"
        )
    lines.append("")
    lines.append("**Summary**")
    lines.append(f"- tasks: {metrics['tasks']}")
    lines.append(
        f"- precision: {metrics['precision']}, recall: {metrics['recall']}, F1: {metrics['f1']}"
    )
    lines.append(f"- false positive rate: {metrics['false_positive_rate']}")
    lines.append(f"- median verify time: {metrics['median_verify_ms']}ms")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-accuracy benchmark for HonestCode.")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "dataset"),
        help="Path to the dataset directory.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "markdown", "json"],
        default="text",
        help="Output format.",
    )
    args = parser.parse_args()

    dataset_dir = Path(args.dataset)
    tasks = load_tasks(dataset_dir)
    if not tasks:
        print(f"No tasks found in {dataset_dir}", file=sys.stderr)
        return 1

    results = [evaluate_task(task) for task in tasks]
    metrics = aggregate(results)

    if args.format == "json":
        print(json.dumps({"metrics": metrics, "results": results}, indent=2))
    elif args.format == "markdown":
        print(render_markdown(results, metrics))
    else:
        print(render_text(results, metrics))

    # Exit non-zero if the benchmark is failing badly enough to block CI.
    if metrics["recall"] < 1.0 or metrics["precision"] < 1.0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
