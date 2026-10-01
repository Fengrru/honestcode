"""Agent-facing verification protocol.

HonestCode's job is not to print lint warnings — it is to hand a coding agent
a piece of **evidence** it can act on without a second round-trip. Every
finding is therefore emitted in a fixed, machine-consumable shape:

.. code-block:: json

    {
      "status": "fail",
      "file": "auth/client.py",
      "line": 42,
      "kind": "invented_api",
      "symbol": "refresh_token",
      "owner": "UserClient",
      "message": "UserClient.refresh_token() does not exist.",
      "evidence": {"available_methods": ["refresh", "refresh_access_token"]},
      "confidence": "deterministic",
      "action": "revise"
    }

Two invariants keep the protocol honest:

* ``confidence: deterministic`` is used only when the verifier could prove the
  claim from the repository alone (the owning class was resolved and its
  member surface is complete). Findings that would have to be inferred through
  an unresolved base class are **not reported at all** — absence cannot be
  proven there, and real-world testing showed that guessing produces
  misleadingly confident false positives (v0.4.0).
* ``action`` tells the agent what to do next, so it does not have to guess
  whether a finding is fatal or advisory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Finding",
    "STATUS_FAIL",
    "STATUS_PASS",
    "build_report",
    "render_text",
]

STATUS_PASS = "pass"
STATUS_FAIL = "fail"

# Finding kinds, ordered by how much an agent should care.
KIND_SYNTAX_ERROR = "syntax_error"
KIND_INVENTED_API = "invented_api"
KIND_WRONG_CALL = "wrong_call"
KIND_UNDEFINED_SYMBOL = "undefined_symbol"

_KIND_ORDER = {
    KIND_SYNTAX_ERROR: 0,
    KIND_INVENTED_API: 1,
    KIND_WRONG_CALL: 2,
    KIND_UNDEFINED_SYMBOL: 3,
}

CONFIDENCE_DETERMINISTIC = "deterministic"
CONFIDENCE_HIGH = "high"

ACTION_REVISE = "revise"
ACTION_INSPECT = "inspect"

# Backwards-compatible ``type`` values for the legacy ``issues`` list.
_LEGACY_TYPE = {
    KIND_SYNTAX_ERROR: "syntax_error",
    KIND_INVENTED_API: "invented_api",
    KIND_WRONG_CALL: "wrong_call",
    KIND_UNDEFINED_SYMBOL: "undefined_call",
}


@dataclass(frozen=True)
class Finding:
    """A single verification failure with enough evidence to fix it."""

    file: str
    line: int
    kind: str
    symbol: str
    message: str
    owner: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str = CONFIDENCE_DETERMINISTIC
    action: str = ACTION_REVISE

    def to_dict(self) -> dict[str, Any]:
        """Return the agent-facing protocol representation."""
        out: dict[str, Any] = {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "symbol": self.symbol,
            "message": self.message,
            "confidence": self.confidence,
            "action": self.action,
        }
        if self.owner is not None:
            out["owner"] = self.owner
        if self.evidence:
            out["evidence"] = self.evidence
        return out

    def legacy(self) -> dict[str, Any]:
        """Return the pre-0.3 ``issues`` shape (``type``/``name``/``line``)."""
        return {
            "type": _LEGACY_TYPE.get(self.kind, self.kind),
            "name": self.symbol,
            "line": self.line,
            "kind": self.kind,
            "owner": self.owner,
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
            "action": self.action,
            "message": self.message,
        }


def build_report(
    file: str,
    findings: list[Finding],
    *,
    checked: int = 0,
    truncated: bool = False,
) -> dict[str, Any]:
    """Wrap *findings* into a full verification report."""
    ordered = sorted(findings, key=lambda f: (_KIND_ORDER.get(f.kind, 99), f.line, f.symbol))
    by_kind: dict[str, int] = {}
    for f in ordered:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    report: dict[str, Any] = {
        "success": True,
        "status": STATUS_FAIL if ordered else STATUS_PASS,
        "file": file,
        "findings": [f.to_dict() for f in ordered],
        "summary": {
            "total": len(ordered),
            "by_kind": by_kind,
            "call_sites_checked": checked,
        },
        # Legacy shape kept so existing clients/CI keep working unchanged.
        "issues": [f.legacy() for f in ordered],
    }
    if truncated:
        report["truncated"] = True
    return report


def render_text(report: dict[str, Any], *, max_methods: int = 8) -> str:
    """Render a report as the compact block an agent (or a human) reads.

    This is deliberately terse: an agent pays tokens for every character, and
    a human scanning a GIF should see the verdict in under a second.
    """
    findings = report.get("findings") or []
    if not findings:
        checked = report.get("summary", {}).get("call_sites_checked", 0)
        return (
            f"✓ verification passed — {report.get('file', '')}\n"
            f"  {checked} call site(s) grounded in the repository · deterministic"
        )

    head = f"✗ verification failed — {len(findings)} issue(s) in {report.get('file', '')}"
    lines = [head]
    for f in findings:
        lines.append("")
        location = f"{f.get('file', '')}:{f.get('line', 0)}"
        lines.append(f"  [{f.get('kind')}] {location}")
        lines.append(f"  {f.get('message')}")
        evidence = f.get("evidence") or {}
        suggestion = evidence.get("did_you_mean")
        if suggestion:
            lines.append(f"  did you mean: {suggestion}()?")
        available = evidence.get("available_methods") or []
        if available:
            lines.append("  available methods:")
            for name in available[:max_methods]:
                lines.append(f"    - {name}()")
            hidden = len(available) - max_methods
            if hidden > 0:
                lines.append(f"    … and {hidden} more")
        expected = evidence.get("expected")
        actual = evidence.get("actual")
        if expected is not None and actual is not None:
            lines.append(f"  expected: {expected}")
            lines.append(f"  actual:   {actual}")
        lines.append(f"  confidence: {f.get('confidence')} · action: {f.get('action')}")
    return "\n".join(lines)
