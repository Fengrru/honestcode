"""Regression tests for the benchmark gates themselves.

A benchmark that cannot fail is documentation with a green checkmark. These
tests pin the two ways the suites used to pass without measuring anything.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_accuracy_counts_spurious_findings_as_false_positives():
    module = _load("agent_accuracy_run", "benchmarks/agent_accuracy/run.py")
    results = [
        {
            "broken": {"detected": True, "spurious_findings": 1, "elapsed_ms": 1.0},
            "fixed": {"clean": True, "findings_count": 0, "elapsed_ms": 1.0},
        }
    ]
    metrics = module.aggregate(results)
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 1
    assert metrics["spurious_findings"] == 1
    assert metrics["precision"] == 0.5


def test_realworld_benchmark_fails_on_an_empty_project(tmp_path, monkeypatch, capsys):
    module = _load("realworld_fp_run", "benchmarks/realworld_fp/run.py")
    project = tmp_path / "empty"
    project.mkdir()
    monkeypatch.setattr(sys, "argv", ["run.py", "--project", str(project)])
    assert module.main() == 2
    assert "no Python files" in capsys.readouterr().out


def test_realworld_benchmark_fails_when_verification_errors(tmp_path, monkeypatch, capsys):
    module = _load("realworld_fp_run_error", "benchmarks/realworld_fp/run.py")
    project = tmp_path / "project"
    project.mkdir()
    (project / "mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "verify_file",
        lambda _path: {"success": False, "error": "cannot read file"},
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--project", str(project)])
    assert module.main() == 2
    assert "could not be verified" in capsys.readouterr().out
