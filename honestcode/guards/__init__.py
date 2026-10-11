"""Crash-safe wrappers for agent side effects.

An agent that retries a tool call after a crash can charge a card twice, send a
message twice, or deploy twice. Durable-execution engines do not solve this for
you — Temporal and Restate both guarantee deterministic *replay of
orchestration* and leave exactly-once external effects to the application. This
package is that piece: classify the effect, key it, persist the key, and refuse
to run it again.

::

    from honestcode.guards import EffectStore, SideEffectGuard

    guard = SideEffectGuard(session_id="sess-1", store_path=".agent/effects.db")

    outcome = guard.run(
        "send_invoice",
        target="cus_123",
        payload={"amount_cents": 4200},
        execute=lambda: billing.send_invoice("cus_123", 4200),
        approve_irreversible=True,
    )

    outcome.executed     # False on the second call — a receipt, not a charge
    outcome.effect_id    # stable across restarts, because the key is on disk

Every effect class is treated as irreversible until classified otherwise, which
is why ``send_invoice`` needs the explicit approval above.

Isolation
---------

This subpackage is deliberately self-contained and is expected to be extracted
into its own distribution:

* It imports **only the standard library** plus its own siblings. It must never
  import ``honestcode.*`` outside ``honestcode.guards.*``, because the rest of
  HonestCode answers questions about code while this package wraps code that
  changes the world — different lifecycle, different blast radius.
* It is **not registered as an MCP tool**. HonestCode's MCP surface is reached
  through client configuration files; a guard belongs in the caller's own
  process, on its own tool-call path.
* It has **no dependency on HonestCode's protocols** — findings, reports, and
  the ``Finding`` shape are all absent here. A guard returns receipts.
"""

from honestcode.guards.guard import (
    EffectNotRunnable,
    ExecutionOutcome,
    GuardError,
    Precondition,
    PreconditionFailed,
    RecoveryDecision,
    SideEffectGuard,
    durable_scope,
    precondition,
    strategy_for,
)
from honestcode.guards.store import (
    ACTIVE_STATUSES,
    COMMITTED,
    COMPENSATED,
    EXECUTING,
    FAILED,
    PENDING,
    EffectRecord,
    EffectStatus,
    EffectStore,
    StoreError,
)
from honestcode.guards.taxonomy import (
    EffectClass,
    RecoveryStrategy,
    classify,
)

__all__ = [
    # taxonomy
    "EffectClass",
    "RecoveryStrategy",
    "classify",
    # store
    "EffectRecord",
    "EffectStatus",
    "EffectStore",
    "StoreError",
    "ACTIVE_STATUSES",
    "PENDING",
    "EXECUTING",
    "COMMITTED",
    "FAILED",
    "COMPENSATED",
    # guard
    "ExecutionOutcome",
    "GuardError",
    "Precondition",
    "PreconditionFailed",
    "EffectNotRunnable",
    "RecoveryDecision",
    "SideEffectGuard",
    "durable_scope",
    "precondition",
    "strategy_for",
]
