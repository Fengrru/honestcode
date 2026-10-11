"""Effect-class taxonomy for agent side effects.

Two properties of this table are deliberate departures from the earlier
implementation this derives from (see :mod:`honestcode.guards.guard` for the
third):

* ``classify`` defaults to *unknown → irreversible*, not → idempotent. The
  2026 consensus across the effect-classification literature is that an
  unrecognised action is the one you must not silently retry; defaulting an
  unknown action to idempotent makes it retryable. Guessing "idempotent" for
  something you do not recognise is the unsafe direction, and the failure it
  produces (a duplicated external mutation) is exactly what this module exists
  to prevent.
* The substring fallback is bounded to word-ish matches rather than a plain
  ``target.contains(...)``, so a path like ``/srv/pushkin/report.txt`` no
  longer classifies as irreversible because it contains ``push``.

Four classes, in increasing severity: ``PURE`` < ``IDEMPOTENT`` <
``REVERSIBLE`` < ``IRREVERSIBLE``. The ordering is meaningful — it is the
default-deny direction — so members are comparable.
"""

from __future__ import annotations

import re
from enum import Enum

__all__ = [
    "EffectClass",
    "RecoveryStrategy",
    "classify",
    "READ_ONLY_ACTIONS",
    "IDEMPOTENT_ACTIONS",
    "REVERSIBLE_ACTIONS",
    "IRREVERSIBLE_ACTIONS",
]


class EffectClass(str, Enum):
    """How bad is it to run this action twice, or to not run it at all?

    ``str``-valued so instances survive JSON round-trips as plain strings,
    which matters because these values are persisted and echoed back to
    callers.
    """

    PURE = "PURE"
    """No observable external change. Safe to repeat without limit."""

    IDEMPOTENT = "IDEMPOTENT"
    """Repeating converges to the same state (``write_file``, ``upsert``)."""

    REVERSIBLE = "REVERSIBLE"
    """Repeating changes state but the change can be undone, given the
    original value (``edit_file``, ``patch``)."""

    IRREVERSIBLE = "IRREVERSIBLE"
    """Repeating causes a second real-world event that cannot be undone
    (``send_email``, ``charge``, ``git_push``, ``deploy``)."""

    def __lt__(self, other: object) -> bool:
        """Compare by severity, so callers can gate on a threshold."""
        if not isinstance(other, EffectClass):
            return NotImplemented
        return _SEVERITY[self] < _SEVERITY[other]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, EffectClass):
            return NotImplemented
        return _SEVERITY[self] <= _SEVERITY[other]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, EffectClass):
            return NotImplemented
        return _SEVERITY[self] > _SEVERITY[other]

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, EffectClass):
            return NotImplemented
        return _SEVERITY[self] >= _SEVERITY[other]

    @property
    def severity(self) -> int:
        return _SEVERITY[self]

    @property
    def bare(self) -> str:
        """Value without the enum prefix — ``"IRREVERSIBLE"`` → ``"irreversible"``.

        The persisted form and the wire form use this, because a caller reading
        a JSON receipt should not have to know about a Python enum.
        """
        return self.value.lower()


_SEVERITY = {
    EffectClass.PURE: 0,
    EffectClass.IDEMPOTENT: 1,
    EffectClass.REVERSIBLE: 2,
    EffectClass.IRREVERSIBLE: 3,
}

# Re-exported for callers that want the raw tables.
_IDEMPOTENT = EffectClass.IDEMPOTENT
_REVERSIBLE = EffectClass.REVERSIBLE
_IRREVERSIBLE = EffectClass.IRREVERSIBLE
_PURE = EffectClass.PURE


class RecoveryStrategy(str, Enum):
    """What to do with an effect whose outcome is unknown, per its class."""

    REPLAY = "replay"
    COMPENSATE = "compensate"
    QUERY_AND_CONFIRM = "query_and_confirm"

    @property
    def bare(self) -> str:
        return self.value


# ── classification tables ───────────────────────────────────────────────────

READ_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "read_file",
        "read",
        "read_text",
        "grep",
        "glob",
        "search",
        "ls",
        "list",
        "list_dir",
        "list_files",
        "calculate",
        "compute",
        "fetch_url",
        "get",
        "stat",
        "diff",
        "view",
    }
)

IDEMPOTENT_ACTIONS: frozenset[str] = frozenset(
    {
        "upsert",
        "replace_text",
        "write_file",
        "write",
        "set",
        "put",
        "create_or_update",
        "ensure",
        "mkdir",
    }
)

REVERSIBLE_ACTIONS: frozenset[str] = frozenset(
    {
        "edit_file",
        "edit",
        "create_file",
        "create",
        "patch",
        "rename",
        "move",
        "copy",
        "insert",
        "append",
    }
)

IRREVERSIBLE_ACTIONS: frozenset[str] = frozenset(
    {
        "send_email",
        "send_message",
        "send",
        "charge",
        "pay",
        "refund",
        "transfer",
        "git_push",
        "push",
        "deploy",
        "release",
        "publish",
        "create_pr",
        "merge",
        "delete",
        "drop",
        "truncate",
    }
)

# Bounded word matches for the fallback path. Substring matching on the raw
# target misfires on paths and identifiers that merely contain a keyword.
_IRREVERSIBLE_HINTS = re.compile(r"(?i)\b(email|mail|push|deploy|charge|refund|payment)\b")
_REVERSIBLE_HINTS = re.compile(r"(?i)\b(file|edit|patch|draft)\b")


def classify(action_type: str, target: str = "") -> EffectClass:
    """Classify an action by name, falling back to hints in *target*.

    The fallback exists because agents call tools whose names this table has
    never seen. When a name is unknown, the target is consulted for a hint, and
    if that yields nothing either, the result is :attr:`EffectClass.IRREVERSIBLE`
    — the safe default. An unrecognised action must be treated as the one that
    cannot be safely retried, because that is the failure you cannot undo.
    """
    name = (action_type or "").strip().lower().replace("-", "_")
    if name in READ_ONLY_ACTIONS:
        return _PURE
    if name in IRREVERSIBLE_ACTIONS:
        return _IRREVERSIBLE
    if name in IDEMPOTENT_ACTIONS:
        return _IDEMPOTENT
    if name in REVERSIBLE_ACTIONS:
        return _REVERSIBLE

    # Unknown action: consult the target for a hint.
    haystack = target or ""
    if _IRREVERSIBLE_HINTS.search(haystack):
        return _IRREVERSIBLE
    if _REVERSIBLE_HINTS.search(haystack):
        return _REVERSIBLE
    return _IRREVERSIBLE
