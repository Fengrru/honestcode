"""pydantic-ai integration for :mod:`honestcode.guards`.

Wraps tool execution so that an effect the framework has already carried out is
not carried out a second time — including across a crash and restart, which is
the case `pydantic-ai` cannot cover by itself.

Why this is a separate package from ``honestcode.guards``:

* ``guards`` is stdlib-only and framework-agnostic. Importing ``pydantic_ai``
  into it would end that, and the isolation rule in ``tests/test_guards.py``
  enforces it.
* The deferral primitives live in the framework. ``pydantic-ai`` already offers
  in-process deferral (``CallDeferred``, ``ApprovalRequired``) from
  ``before_tool_execute``; those are the right tool for *a human should look at
  this*. This integration is for the different question: *did the outside world
  already change, and I no longer remember*. That question survives a process
  restart, so it needs the durable store.

The hook, and why it works
--------------------------

``wrap_tool_execute`` wraps the whole lifecycle. Returning without calling
``handler()`` skips execution entirely and the returned value becomes the tool
result the model sees — verified against ``pydantic-ai`` 2.55.0: with
``handler`` called the tool runs once, with it skipped the tool runs zero times
and the substitute value reaches the model.

That is exactly the shape an idempotency guard needs. What this class adds over
the framework's own deferral is *memory that outlives the process*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from honestcode.guards import (
    EffectClass,
    EffectStore,
    SideEffectGuard,
    classify,
    durable_scope,
)
from honestcode.guards.guard import EffectNotRunnable, GuardError

try:  # pragma: no cover - import guard only
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.exceptions import ModelRetry
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "honestcode.integrations.pydantic_ai requires pydantic-ai; "
        "install it with `pip install honestcode[pydantic-ai]`"
    ) from exc

if TYPE_CHECKING:  # pragma: no cover
    from pydantic_ai.tools import ToolDefinition

__all__ = ["EffectGuardCapability", "ChargePolicy", "default_policy"]


def _as_mapping(value: Any) -> dict[str, Any] | None:
    """Coerce tool-call arguments into a mapping, or ``None`` if impossible.

    ``ToolCallPart.args`` is typed as a dict but arrives as a **JSON string**
    while streaming — which is the normal case for a real provider, not an edge
    case. Treating the string form as unkeyable made the guard pass every real
    call straight through while reporting nothing, so this accepts both.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# ── policy ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChargePolicy:
    """Which tool calls count as effects worth guarding, and what may run.

    A guard that wraps every tool call is noise: ``read_file`` and
    ``list_directory`` are not effects, and recording them grows the store while
    adding nothing. This decides what is worth a durable record.

    The defaults are deliberately narrow — guard what can hurt, let reads
    through untouched, refuse irreversible calls until someone opts in. Only the
    caller knows which of their tools reach the outside world, so widening this
    is their decision.
    """

    approve_irreversible: bool = False
    """Whether irreversible effects may run.

    Defaults to refusing, so the first attempt raises a retry asking for
    approval rather than charging a card. The guard's own default, restated here
    because this is the layer where an application is most likely to want the
    blanket decision.
    """

    approve: Callable[[str, Mapping[str, Any]], bool] | None = None
    """Optional per-call approval, consulted when ``approve_irreversible`` is
    False. Lets an application decide on the specifics — an order's status, a
    spend limit, an allowlist — instead of approving a whole class. Returning
    True for one call does not approve the next."""

    guard_classes: frozenset[EffectClass] = frozenset(
        {EffectClass.REVERSIBLE, EffectClass.IRREVERSIBLE}
    )
    """Effect classes that get a durable record. Reads are excluded by default."""

    classify: Callable[[str, str], EffectClass] = classify
    """How to decide the class. Defaults to the shared action-name table."""

    read_only_tools: frozenset[str] = frozenset()
    """Tool names the application declares to be reads.

    The shared table describes generic names; it cannot know an application's
    own vocabulary. Without a declaration here, a tool whose name the table does
    not recognise falls to the conservative default — irreversible — and is then
    refused unless approved, which for a local file read is a false positive
    that costs an approval.

    Declaring is a deliberate act, and that is the point: only the application
    knows whether `search_docs` hits a local index or a paid API.
    """

    def applies_to(self, effect_class: EffectClass) -> bool:
        return effect_class in self.guard_classes

    def class_of(self, tool_name: str, target: str) -> EffectClass:
        """The effect class for a call, honouring declared reads."""
        if tool_name in self.read_only_tools:
            return EffectClass.PURE
        return self.classify(tool_name, target)

    def approves(self, tool_name: str, args: Mapping[str, Any]) -> bool:
        if self.approve_irreversible:
            return True
        if self.approve is not None:
            return bool(self.approve(tool_name, args))
        return False


def default_policy() -> ChargePolicy:
    return ChargePolicy()


# ── capability ──────────────────────────────────────────────────────────────


@dataclass
class EffectGuardCapability(AbstractCapability):  # type: ignore[misc]
    """Make agent tool calls idempotent across a crash and restart.

    Usage::

        from honestcode.guards import EffectStore
        from honestcode.integrations.pydantic_ai import EffectGuardCapability

        cap = EffectGuardCapability(
            store=EffectStore(".agent/effects.db"),
            run_id="order-4711-fulfilment",   # stable across retries
        )
        agent = Agent(model, tools=[charge_card], capabilities=[cap])

    ``run_id`` is the durability boundary and the field that matters most. It
    must be the identifier the application can reproduce after a restart — a
    workflow run id, a job id, an order id. Passing a fresh id on every attempt
    turns the guard into a very expensive no-op, because a new id means every
    retry looks like a new effect.
    """

    store: EffectStore
    run_id: str
    policy: ChargePolicy = field(default_factory=default_policy)

    receipts: dict[str, str] = field(default_factory=dict)
    """Effect id per tool call id, for observability after a run."""

    unguarded_calls: list[str] = field(default_factory=list)
    """Tool names that passed through because their arguments were unkeyable.

    Non-empty after a run means the guard did *not* protect those calls. That is
    the failure mode this attribute exists to make visible: the first version of
    this file let every real call through this way and recorded nothing.
    """

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty — it is the durability boundary")
        self._guard = SideEffectGuard(session_id=f"pydantic-ai:{self.run_id}", store=self.store)

    # ── the hook ────────────────────────────────────────────────────────────

    async def wrap_tool_execute(
        self,
        ctx: Any,
        *,
        call: Any,
        tool_def: ToolDefinition,
        args: Any,
        handler: Any,
    ) -> Any:
        tool_name = getattr(call, "tool_name", "") or getattr(tool_def, "name", "")
        call_args = _as_mapping(getattr(call, "args", None))
        if call_args is None:
            # Args are absent or unparseable. Without a mapping we cannot key
            # the effect, and guessing risks two different effects sharing a
            # key, so the call is let through unguarded — but silently, which is
            # exactly how the first version of this file shipped a guard that
            # never ran. Record the skip so it is visible in a test or a log.
            self.unguarded_calls.append(tool_name)
            return await handler(args)

        target = _target_of(tool_name, call_args)
        effect_class = self.policy.class_of(tool_name, target)

        if not self.policy.applies_to(effect_class):
            return await handler(args)

        scope = durable_scope(self.run_id, tool_name, target)
        approved = effect_class is not EffectClass.IRREVERSIBLE or self.policy.approves(
            tool_name, call_args
        )

        async def call_handler() -> Any:
            return await handler(args)

        try:
            outcome = await self._guard.arun(
                tool_name,
                target=target,
                payload=call_args,
                effect_class=effect_class,
                approve_irreversible=approved,
                scope=scope,
                # The handler is async, and arun writes the `executing` claim
                # before awaiting it — so a crash while the tool is in flight
                # leaves an unknown outcome rather than looking unstarted.
                execute=call_handler,
            )
        except EffectNotRunnable as exc:
            raise ModelRetry(_stuck_message(exc, tool_name, target)) from exc
        except GuardError as exc:
            raise ModelRetry(_refused_message(exc, tool_name, target)) from exc

        tool_call_id = getattr(call, "tool_call_id", "")
        if tool_call_id:
            self.receipts[tool_call_id] = outcome.effect_id

        if outcome.deduplicated:
            return _receipt(tool_name, target, effect_class, outcome)
        return outcome.value


def _target_of(tool_name: str, args: Mapping[str, Any]) -> str:
    """A short, stable description of *what* the effect acts on.

    Used in the scope and in receipts. Picks the first recognisable identifier
    in the arguments rather than dumping them all, so the scope stays stable
    against irrelevant argument churn.
    """
    for key in ("target", "path", "customer", "customer_id", "order_id", "id", "name"):
        if key in args and isinstance(args[key], str | int):
            return f"{key}={args[key]}"
    return tool_name


def _receipt(tool_name: str, target: str, effect_class: EffectClass, outcome: Any) -> str:
    return (
        f"[effect-guard] {tool_name}({target}) was NOT run again — this "
        f"{effect_class.bare} effect already completed in run "
        f"{outcome.record.effect_id}. The previous result stands. If you need a "
        f"genuinely new effect, change the argument that identifies it."
    )


def _refused_message(exc: GuardError, tool_name: str, target: str) -> str:
    return (
        f"[effect-guard] {tool_name}({target}) was not run: {exc}. "
        f"Resolve the condition and retry, or use a different action."
    )


def _stuck_message(exc: EffectNotRunnable, tool_name: str, target: str) -> str:
    return (
        f"[effect-guard] {tool_name}({target}) has an unknown outcome from an "
        f"interrupted attempt ({exc.status}). It may already have reached the "
        f"outside world. Do not retry it blindly — it needs to be reconciled "
        f"with the external system first."
    )
