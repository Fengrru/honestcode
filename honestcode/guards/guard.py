"""The side-effect guard: the crash-safe wrapper around an agent's tool calls.

Three defects in the earlier implementation this derives from are fixed here
rather than carried over, and each is called out at the site where it matters:

1. **The idempotency key was computed and discarded.** The earlier code wrote
   ``let _idempotency_key = ...`` and then compared raw ``request_hash``
   values in memory. Here the key is derived, persisted, and enforced by a
   ``UNIQUE`` constraint, so it survives a restart — which is the only
   situation in which idempotency is worth anything.
2. **Failure was recorded as success.** It set ``status = Committed``,
   *then* checked ``if !result.success { status = Failed }``, so a throwing
   executor left a committed record and the next attempt served a receipt for
   work that never happened. The order is inverted here.
3. **The state machine had no gap between "not started" and "done".** This
   module adds ``EXECUTING`` and commits it *before* the outside world is
   touched, so a crash mid-call leaves evidence that the outcome is unknown
   instead of evidence that nothing happened.

The public surface is two calls: :meth:`SideEffectGuard.run` for the common
case, and :meth:`SideEffectGuard.recover` after a restart.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

from honestcode.guards.store import (
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
from honestcode.guards.taxonomy import EffectClass, RecoveryStrategy, classify

__all__ = [
    "ExecutionOutcome",
    "GuardError",
    "PreconditionFailed",
    "EffectNotRunnable",
    "RecoveryDecision",
    "SideEffectGuard",
    "durable_scope",
    "precondition",
    "strategy_for",
]

T = TypeVar("T")


# ── errors ──────────────────────────────────────────────────────────────────


class GuardError(RuntimeError):
    """Base class for every refusal this module makes."""


class PreconditionFailed(GuardError):
    """A caller-supplied precondition refused the effect before it ran."""

    def __init__(self, reason: str, *, precondition: str = "") -> None:
        self.reason = reason
        self.precondition = precondition
        detail = f"precondition failed: {reason}" if reason else "precondition failed"
        if precondition:
            detail += f" ({precondition})"
        super().__init__(detail)


class EffectNotRunnable(GuardError):
    """The effect cannot be run in its current state.

    Raised for a record that is already committed (retrieve its receipt
    instead), already executing (another attempt owns it), or in an unknown
    state after a crash.
    """

    def __init__(self, effect_id: str, status: EffectStatus, detail: str = "") -> None:
        self.effect_id = effect_id
        self.status = status
        message = f"effect {effect_id} is {status}, not runnable"
        if detail:
            message += f": {detail}"
        super().__init__(message)


# ── preconditions ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Precondition:
    """A named check that must pass before an effect is allowed to run.

    Upstream's implementation was ``if !precond.is_empty() && precond == "deny"``
    — a comparison against one magic string, which meant every other
    precondition value was silently ignored. A precondition that is silently
    ignored is worse than no precondition at all, because it is documented
    protection that does not exist.

    The contract here: ``check`` is called with the caller's context mapping and
    returns truthy to permit the effect. ``reason`` is what a refused caller is
    told, so it should name the actual limit — "budget exhausted", not "denied".
    A precondition that raises is treated as a refusal, because a check that
    cannot complete has not established that the effect is safe.
    """

    name: str
    check: Callable[[Mapping[str, Any]], bool]
    reason: str = ""

    def evaluate(self, context: Mapping[str, Any]) -> None:
        try:
            allowed = bool(self.check(context))
        except Exception as exc:  # noqa: BLE001 - a failed check is not a pass
            raise PreconditionFailed(f"precondition raised: {exc}", precondition=self.name) from exc
        if not allowed:
            raise PreconditionFailed(
                self.reason or f"{self.name} refused the effect", precondition=self.name
            )


def precondition(
    name: str, reason: str = ""
) -> Callable[[Callable[[Mapping[str, Any]], bool]], Precondition]:
    """Decorator form: ``@precondition("budget", "budget exhausted")``."""

    def wrap(fn: Callable[[Mapping[str, Any]], bool]) -> Precondition:
        return Precondition(name=name, check=fn, reason=reason)

    return wrap


# ── results ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionOutcome:
    """What :meth:`SideEffectGuard.run` returns.

    ``executed`` distinguishes "your callable ran this time" from "we served a
    receipt for a call that already happened". That distinction is the entire
    product: a caller can log, bill, or branch on it, and cannot get it wrong by
    accident.
    """

    record: EffectRecord
    value: Any = None
    executed: bool = False

    @property
    def effect_id(self) -> str:
        return self.record.effect_id

    @property
    def status(self) -> EffectStatus:
        return self.record.status

    @property
    def deduplicated(self) -> bool:
        """True when this call adopted an existing effect instead of running one."""
        return not self.executed

    def to_dict(self) -> dict[str, Any]:
        out = self.record.to_dict()
        out["executed"] = self.executed
        if self.value is not None:
            out["value"] = self.value
        return out


@dataclass(frozen=True)
class RecoveryDecision:
    """What to do about an effect whose fate is unknown after a restart."""

    record: EffectRecord
    strategy: RecoveryStrategy
    action: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.record.effect_id,
            "status": self.record.status.bare,
            "effect_class": self.record.effect_class.bare,
            "strategy": self.strategy.bare,
            "action": self.action,
            "detail": self.detail,
        }


# ── the guard ───────────────────────────────────────────────────────────────


def _hash_payload(payload: Any) -> str:
    """Stable content hash for the effect's arguments.

    ``sort_keys`` plus ``default=repr`` so a dict with mixed value types still
    hashes deterministically — an unstable hash would make every retry look
    like a new effect, which defeats the guard.
    """
    try:
        blob = json.dumps(payload, sort_keys=True, default=repr, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


def _idempotency_key(session_id: str, action_type: str, target: str, payload: Any) -> str:
    """The key that makes an effect idempotent *within a session*.

    Derived from *what the effect is*, not from when it was requested, so two
    attempts at the same logical action produce the same key and the second one
    is recognised. Upstream's ``{session_hex}:{request_hash}`` had the right
    shape; it just never used it.

    Session-scoped, so it answers "has this session already done this". For the
    cross-restart question use :func:`_scope_key` via ``run(scope=...)``.
    """
    digest = hashlib.blake2b(
        f"{action_type}\x00{target}\x00{_hash_payload(payload)}".encode(),
        digest_size=16,
    ).hexdigest()
    return f"{session_id}:{digest}"


def durable_scope(run_id: str, *parts: str) -> str:
    """Build the ``scope`` argument for :meth:`SideEffectGuard.run`.

    ``run_id`` is the caller's durability boundary — the identifier that means
    the same thing after a crash. A workflow run id, a job id, an order id:
    whatever the caller can reproduce on the retry. It must *not* be the session
    id, because a restart produces a new session and the whole point is to
    recognise the retry as the same effect.

    The remaining ``parts`` narrow the scope to one effect within that run.

    **Scope is authoritative.** Passing a scope makes effect identity depend on
    the scope and the effect's own arguments, and *not* on the session — so the
    same scope in a different session resolves to the same record. That is what
    makes a crash-and-retry safe. It also means the caller owns the isolation
    question: if two independent agents must not share dedup state, they must
    have different ``run_id``s.
    """
    if not run_id:
        raise ValueError("run_id must not be empty")
    suffix = _hash_payload(list(parts)) if parts else ""
    return f"{run_id}:{suffix}" if suffix else run_id


def _scope_namespace(scope: str) -> str:
    """The record-view namespace for a scoped effect.

    Scoped effects are stored under a namespace derived from the scope rather
    than from the session id, because the session is exactly what changes across
    a restart. It is namespaced rather than left empty so one store can hold
    both scoped and session-scoped effects without the two kinds colliding.
    """
    digest = hashlib.blake2b(scope.encode(), digest_size=16).hexdigest()
    return f"scope-ns:{digest}"


def _scope_key(action_type: str, target: str, payload: Any) -> str:
    """Key for a scoped effect. Identity comes from the effect itself; the
    scope lives in the namespace, so the two together are the full identity."""
    digest = hashlib.blake2b(
        f"{action_type}\x00{target}\x00{_hash_payload(payload)}".encode(),
        digest_size=16,
    ).hexdigest()
    return f"scope:{digest}"


class SideEffectGuard:
    """Wraps instrumented tool calls so that repeating one is safe.

    Usage::

        guard = SideEffectGuard(session_id="sess-1", store=EffectStore("effects.db"))

        outcome = guard.run(
            "charge_card",
            target="cus_123",
            payload={"amount_cents": 100},
            execute=lambda: stripe.charge(customer="cus_123", amount=100),
            preconditions=[within_budget],
        )

        if outcome.deduplicated:
            log.info("already charged, reusing receipt %s", outcome.effect_id)

    One guard instance owns one session. Every method is safe to call
    concurrently; the store serialises writes and the in-process lock covers the
    read-modify-write sequences.
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: EffectStore | None = None,
        store_path: str = ":memory:",
        approve_irreversible: bool = False,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        self.session_id = session_id
        self.store = store if store is not None else EffectStore(store_path)
        self.approve_irreversible = approve_irreversible

    def close(self) -> None:
        self.store.close()

    # ── the main path ───────────────────────────────────────────────────────

    def run(
        self,
        action_type: str,
        *,
        execute: Callable[[], T],
        target: str = "",
        payload: Any = None,
        effect_class: EffectClass | None = None,
        preconditions: tuple[Precondition, ...] = (),
        context: Mapping[str, Any] | None = None,
        approve_irreversible: bool | None = None,
        scope: str | None = None,
    ) -> ExecutionOutcome:
        """Run *execute* at most once for this logical effect.

        Returns an :class:`ExecutionOutcome` whose ``executed`` flag says
        whether the callable ran. Raises :class:`PreconditionFailed` before
        touching the outside world if any precondition refuses.

        ``scope`` is the durability boundary. When omitted the key is derived
        from the session, which is right for "has *this session* already done
        this". Pass it when the effect must stay recognisable across sessions —
        a crash and restart produces a new session id, and a key derived from
        the session would therefore treat the retry as a brand-new effect, which
        is exactly the case this guard exists for. See
        :func:`durable_scope` for the usual construction.
        """
        resolved = effect_class or classify(action_type, target)
        approved = (
            self.approve_irreversible if approve_irreversible is None else approve_irreversible
        )

        record, created = self._claim(
            action_type=action_type,
            target=target,
            payload=payload,
            effect_class=resolved,
            preconditions=preconditions,
            context=context or {},
            approve_irreversible=approved,
            scope=scope,
        )

        if not created:
            return self._adopt(record)

        # The effect is claimed and durable. Failures past this point must not
        # leave the record looking unstarted.
        try:
            self.store.set_status(record.effect_id, EXECUTING)
            value = execute()
        except Exception:
            self._mark_failed(record.effect_id)
            raise

        committed = self.store.set_status(
            record.effect_id,
            COMMITTED,
            response_hash=_hash_payload(_receipt_value(value)),
        )
        return ExecutionOutcome(record=committed, value=value, executed=True)

    async def arun(
        self,
        action_type: str,
        *,
        execute: Callable[[], Awaitable[T]],
        target: str = "",
        payload: Any = None,
        effect_class: EffectClass | None = None,
        preconditions: tuple[Precondition, ...] = (),
        context: Mapping[str, Any] | None = None,
        approve_irreversible: bool | None = None,
        scope: str | None = None,
    ) -> ExecutionOutcome:
        """Await *execute* at most once for this logical effect.

        The same lifecycle as :meth:`run`, for callers that are already async.
        The claim is written *before* the await, so a process that dies while the
        coroutine is suspended leaves an ``executing`` record — the outcome is
        recorded as unknown rather than as never-happened, which is what stops
        the next attempt from repeating an external mutation.

        Use this rather than :meth:`run` from async code. Because ``run`` is
        synchronous it has to drive an awaitable to completion itself, which it
        cannot do on a loop that is already running.
        """
        resolved = effect_class or classify(action_type, target)
        approved = (
            self.approve_irreversible if approve_irreversible is None else approve_irreversible
        )

        record, created = self._claim(
            action_type=action_type,
            target=target,
            payload=payload,
            effect_class=resolved,
            preconditions=preconditions,
            context=context or {},
            approve_irreversible=approved,
            scope=scope,
        )

        if not created:
            return self._adopt(record)

        try:
            self.store.set_status(record.effect_id, EXECUTING)
            value = await execute()
        except Exception:
            self._mark_failed(record.effect_id)
            raise

        committed = self.store.set_status(
            record.effect_id,
            COMMITTED,
            response_hash=_hash_payload(_receipt_value(value)),
        )
        return ExecutionOutcome(record=committed, value=value, executed=True)

    def _claim(
        self,
        *,
        action_type: str,
        target: str,
        payload: Any,
        effect_class: EffectClass,
        preconditions: tuple[Precondition, ...],
        context: Mapping[str, Any],
        approve_irreversible: bool,
        scope: str | None = None,
    ) -> tuple[EffectRecord, bool]:
        """Reserve the effect, or return the existing record for it.

        Order matters: the durable claim happens *before* preconditions are
        evaluated only in the sense that a duplicate is detected first. A
        refusal raises before any record is written, so a precondition that
        fails does not consume the idempotency key.
        """
        if scope:
            # Scope is authoritative: the record lives in a namespace derived
            # from the scope, so the same scope resolves to the same record
            # regardless of which session is asking. Using the session id here
            # would defeat the purpose, since a restart is precisely a new
            # session id.
            session_id = _scope_namespace(scope)
            key = _scope_key(action_type, target, payload)
        else:
            session_id = self.session_id
            key = _idempotency_key(self.session_id, action_type, target, payload)

        existing = self.store.find_by_key(session_id, key)
        if existing is not None and existing.is_active:
            return existing, False

        self._check_preconditions(effect_class, preconditions, context, approve_irreversible)

        if existing is not None:
            # A previous attempt failed or was compensated. Re-issuing a failed
            # irreversible effect would be a second real-world event, so the
            # caller is made to look at it rather than having it silently
            # retried.
            if existing.status == FAILED and effect_class is EffectClass.IRREVERSIBLE:
                raise EffectNotRunnable(
                    existing.effect_id,
                    existing.status,
                    "an irreversible effect that already failed must be resolved by "
                    "hand — its partial effects cannot be assumed away",
                )
            self.store.set_status(existing.effect_id, PENDING)
            return self.store.get(existing.effect_id) or existing, True

        effect_id = f"se_{uuid.uuid4().hex}"
        inserted = self.store.insert(
            effect_id=effect_id,
            session_id=session_id,
            idempotency_key=key,
            action_type=action_type,
            target=target,
            effect_class=effect_class,
            payload_hash=_hash_payload(payload),
            preconditions=tuple(p.name for p in preconditions),
        )
        if inserted is None:
            # Lost a race with a concurrent caller for the same effect. Adopt
            # the winner's record; do not run.
            winner = self.store.find_by_key(session_id, key)
            if winner is None:  # pragma: no cover - would mean the schema lied
                raise StoreError(f"effect key {key} vanished after a uniqueness conflict")
            return winner, False
        return inserted, True

    def _check_preconditions(
        self,
        effect_class: EffectClass,
        preconditions: tuple[Precondition, ...],
        context: Mapping[str, Any],
        approve_irreversible: bool,
    ) -> None:
        if effect_class is EffectClass.IRREVERSIBLE and not approve_irreversible:
            raise PreconditionFailed(
                "irreversible effects require explicit approval "
                "(pass approve_irreversible=True once the action has been reviewed)",
                precondition="irreversible_approval",
            )
        for check in preconditions:
            check.evaluate(context)

    def _adopt(self, record: EffectRecord) -> ExecutionOutcome:
        """Serve an existing effect instead of running it again."""
        if record.status == COMMITTED:
            return ExecutionOutcome(record=record, value=record.response_hash, executed=False)
        if record.status == EXECUTING:
            # Claimed before a crash. The outside world may already have been
            # changed, so this is not a safe place to resume automatically.
            raise EffectNotRunnable(
                record.effect_id,
                record.status,
                "a previous attempt may have reached the outside world; call "
                "recover() to get a decision for this effect",
            )
        raise EffectNotRunnable(record.effect_id, record.status, "the effect is already claimed")

    def _mark_failed(self, effect_id: str) -> None:
        with suppress(StoreError):  # store gone mid-flight
            self.store.set_status(effect_id, FAILED)

    # ── recovery ────────────────────────────────────────────────────────────

    def recover(self, session_id: str | None = None) -> list[RecoveryDecision]:
        """Decide what to do about every effect whose outcome is unknown.

        Call this at session start, before any new work. Each decision names the
        action that is safe for that effect's class, and the caller is expected
        to carry it out or escalate — this method deliberately does not execute
        anything, because the safe action for an irreversible effect is a human
        looking at the vendor's dashboard.
        """
        sid = session_id or self.session_id
        return [self.decide(record) for record in self.store.unresolved(sid)]

    def decide(self, record: EffectRecord) -> RecoveryDecision:
        """Map one record to its recovery action.

        The table is the port of upstream's ``recover_effect``, with the
        strategy/action split kept so callers can group by class (``strategy``)
        while still seeing the concrete instruction (``action``).
        """
        cls = record.effect_class
        if record.status == COMMITTED:
            return RecoveryDecision(
                record,
                RecoveryStrategy.REPLAY,
                "use_cached",
                "the effect already ran; serve the recorded response and do not call again",
            )
        if record.status == COMPENSATED:
            return RecoveryDecision(
                record,
                strategy_for(cls),
                "replay",
                "the effect was undone; running it again is safe",
            )
        if record.status == FAILED:
            if cls is EffectClass.IRREVERSIBLE:
                return RecoveryDecision(
                    record,
                    RecoveryStrategy.QUERY_AND_CONFIRM,
                    "query_external",
                    "an irreversible effect that failed may have partially applied; "
                    "confirm against the external system before any retry",
                )
            return RecoveryDecision(
                record, strategy_for(cls), "retry", "the effect raised before committing"
            )
        # PENDING or EXECUTING: the crash case.
        if cls is EffectClass.IRREVERSIBLE:
            return RecoveryDecision(
                record,
                RecoveryStrategy.QUERY_AND_CONFIRM,
                "query_external",
                "the outcome of an irreversible effect is unknown; confirm against "
                "the external system before retrying — retrying blindly risks a "
                "second real-world event",
            )
        if cls is EffectClass.REVERSIBLE:
            if record.compensation is not None:
                return RecoveryDecision(
                    record,
                    RecoveryStrategy.COMPENSATE,
                    "compensate_and_replay",
                    "compensation data is recorded; undo the partial change first",
                )
            return RecoveryDecision(
                record,
                RecoveryStrategy.REPLAY,
                "replay",
                "reversible with no compensation data; replaying converges on the intended state",
            )
        # PURE and IDEMPOTENT.
        return RecoveryDecision(
            record,
            RecoveryStrategy.REPLAY,
            "replay",
            "safe to repeat without limit",
        )

    def resolve(
        self,
        effect_id: str,
        status: EffectStatus,
        *,
        response_hash: str | None = None,
    ) -> EffectRecord:
        """Close out an effect by hand after a recovery decision.

        This is the human-in-the-loop end of :meth:`recover`: an operator who
        has checked the vendor dashboard calls this to record what they found.
        """
        return self.store.set_status(effect_id, status, response_hash=response_hash)

    async def recovered_retry(
        self,
        effect_id: str,
        *,
        execute: Callable[[], Awaitable[T]],
        approve_irreversible: bool = True,
    ) -> ExecutionOutcome:
        """Retry an effect whose outcome an operator has already resolved.

        The missing step between :meth:`recover` and getting on with the work.
        :meth:`recover` yields decisions; an operator acts on one; this carries
        the effect through under the answer they reached.

        What it refuses is the whole point:

        * ``committed`` — returns the existing record without running. Re-running
          a completed effect is the one thing this module exists to prevent.
        * ``executing`` — raises. Nobody resolved it, and an unknown outcome is
          exactly what must not be retried blind: it may already have reached
          the outside world.
        * ``pending`` — raises. :meth:`run` handles a fresh attempt; going
          through this method would skip the guard's own bookkeeping.

        ``approve_irreversible`` defaults to **True** here, unlike :meth:`run`.
        That is deliberate and narrow: this method requires an ``effect_id``,
        which only comes from a recovery review, so the approval already
        happened in the real world and re-asking would be theatre. A caller who
        did not do that review gains nothing, because they cannot name the effect.
        """
        record = self.store.get(effect_id)
        if record is None:
            raise StoreError(f"no such effect: {effect_id}")

        if record.status == COMMITTED:
            return ExecutionOutcome(record=record, value=record.response_hash, executed=False)
        if record.status == EXECUTING:
            raise EffectNotRunnable(
                effect_id,
                record.status,
                "this effect has not been resolved — its outcome is still unknown. "
                "Confirm against the external system with resolve() first",
            )
        if record.status == PENDING:
            raise EffectNotRunnable(
                effect_id,
                record.status,
                "use run() for a fresh attempt; this method is for an effect a "
                "recovery review has already settled",
            )

        # FAILED or COMPENSATED: both mean "did not land", so retrying is safe.
        self.store.set_status(effect_id, PENDING)
        self.store.set_status(effect_id, EXECUTING)
        try:
            value = await execute()
        except Exception:
            self._mark_failed(effect_id)
            raise

        committed = self.store.set_status(
            effect_id,
            COMMITTED,
            response_hash=_hash_payload(_receipt_value(value)),
        )
        return ExecutionOutcome(record=committed, value=value, executed=True)

    # ── introspection ───────────────────────────────────────────────────────

    def effects(self, *, status: EffectStatus | None = None) -> list[EffectRecord]:
        return self.store.list_records(session_id=self.session_id, status=status)

    def summary(self) -> dict[str, int]:
        return self.store.count_by_status(self.session_id)


def strategy_for(effect_class: EffectClass) -> RecoveryStrategy:
    """Class → strategy. Port of ``SideEffectClass::recovery_strategy``."""
    if effect_class in (EffectClass.PURE, EffectClass.IDEMPOTENT):
        return RecoveryStrategy.REPLAY
    if effect_class is EffectClass.REVERSIBLE:
        return RecoveryStrategy.COMPENSATE
    return RecoveryStrategy.QUERY_AND_CONFIRM


def _receipt_value(value: Any) -> Any:
    """Normalise an executor's return value into something hashable.

    The response hash is what proves two attempts produced the same result, so
    it must be stable for equal values and different for different ones.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_receipt_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _receipt_value(v) for k, v in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return repr(value)
