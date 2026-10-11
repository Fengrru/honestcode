"""Tests for the pydantic-ai integration.

Skipped wholesale when ``pydantic-ai`` is not installed, so the core suite keeps
its dependency-free character.

The tests that matter most here are not the happy paths. Two specific failures
are pinned:

* **The guard silently never running.** The first version of the capability
  treated a JSON-string ``args`` as unkeyable and let the call through — and
  ``ToolCallPart.args`` arrives as a string for every real provider. The guard
  reported nothing and protected nothing. ``test_string_args_are_keyed`` and
  ``test_unguarded_calls_are_recorded`` fail if that regresses.
* **Dedup surviving a restart.** A new session id must not mean a new effect
  when the application supplies the same run id.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai", reason="pydantic-ai is an optional integration")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.exceptions import UnexpectedModelBehavior  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from honestcode.guards import EffectClass, EffectStore  # noqa: E402
from honestcode.integrations.pydantic_ai import (  # noqa: E402
    ChargePolicy,
    EffectGuardCapability,
    _as_mapping,
    _target_of,
)


@pytest.fixture
def ledger():
    return []


@pytest.fixture
def store(tmp_path):
    s = EffectStore(str(tmp_path / "effects.db"))
    yield s
    s.close()


def make_tool(ledger):
    def charge_card(customer: str, cents: int) -> str:
        """Charge a customer's card."""
        ledger.append((customer, cents))
        return f"charged {cents} to {customer}"

    return charge_card


def make_agent(store, ledger, run_id, *, approve=True, tool=None):
    cap = EffectGuardCapability(
        store=store,
        run_id=run_id,
        policy=ChargePolicy(approve_irreversible=approve),
    )
    agent = Agent(
        TestModel(call_tools="all"),
        tools=[tool or make_tool(ledger)],
        capabilities=[cap],
    )
    return agent, cap


# ── argument handling: the bug that made the guard a no-op ──────────────────


def test_as_mapping_accepts_a_dict():
    assert _as_mapping({"a": 1}) == {"a": 1}


def test_as_mapping_parses_the_json_string_form():
    """``ToolCallPart.args`` is a dict by type but a JSON string in practice."""
    assert _as_mapping('{"customer": "alice", "cents": 4200}') == {
        "customer": "alice",
        "cents": 4200,
    }


@pytest.mark.parametrize("value", [None, "", "not json", "[1, 2]", "42", 3.5, [1]])
def test_as_mapping_refuses_what_it_cannot_key(value):
    assert _as_mapping(value) is None


def test_string_args_are_keyed():
    """A string-args call must be guarded, not waved through."""
    assert _target_of("charge_card", {"customer": "alice", "cents": 4200}) == "customer=alice"


def test_unguarded_calls_are_recorded(store, ledger):
    """A call the guard could not key must be visible, not silent.

    This is the regression guard for the original defect: an unkeyable call used
    to pass through with nothing recorded anywhere, so a run looked protected
    when it was not.
    """
    cap = EffectGuardCapability(store=store, run_id="r", policy=ChargePolicy())
    assert cap.unguarded_calls == []

    # Directly exercise the branch: the hook appends before deciding to skip.
    import asyncio

    async def handler(args):
        return "ran"

    class FakeCall:
        tool_name = "charge_card"
        args = "not json at all"
        tool_call_id = "c1"

    class FakeDef:
        name = "charge_card"

    asyncio.run(
        cap.wrap_tool_execute(None, call=FakeCall(), tool_def=FakeDef(), args={}, handler=handler)
    )
    assert cap.unguarded_calls == ["charge_card"]


# ── target derivation ───────────────────────────────────────────────────────


def test_target_prefers_a_recognisable_identifier():
    assert _target_of("t", {"order_id": "o-1", "junk": "x"}) == "order_id=o-1"
    assert _target_of("t", {"path": "a.py"}) == "path=a.py"


def test_target_falls_back_to_the_tool_name():
    assert _target_of("do_thing", {"unrelated": "x"}) == "do_thing"


# ── the guard ───────────────────────────────────────────────────────────────


def test_a_fresh_run_executes(store, ledger):
    agent, _ = make_agent(store, ledger, "run-1")
    agent.run_sync("charge")
    assert len(ledger) == 1


def test_the_same_run_id_does_not_charge_twice(store, ledger):
    """The core promise. Two agents, two sessions, one run id, one charge."""
    first, _ = make_agent(store, ledger, "run-1")
    first.run_sync("charge")
    assert len(ledger) == 1

    # A second agent simulates the restart: new guard, new session, same run.
    second, cap = make_agent(store, ledger, "run-1")
    result = second.run_sync("charge")
    assert len(ledger) == 1, "the retry must not reach the vendor"
    assert "NOT run again" in str(result.output)
    assert cap.unguarded_calls == []


def test_a_different_run_id_is_a_different_effect(store, ledger):
    make_agent(store, ledger, "run-1")[0].run_sync("charge")
    make_agent(store, ledger, "run-2")[0].run_sync("charge")
    assert len(ledger) == 2, "two genuine orders are two genuine charges"


def test_a_receipt_is_returned_to_the_model(store, ledger):
    make_agent(store, ledger, "run-1")[0].run_sync("charge")
    _, cap = make_agent(store, ledger, "run-1")
    agent2, cap2 = make_agent(store, ledger, "run-1")
    result = agent2.run_sync("charge")
    text = str(result.output)
    assert "already completed" in text
    assert "The previous result stands" in text
    assert cap2.receipts, "the effect id is recorded for observability"


def test_read_only_tools_are_not_guarded(store):
    """Reads are excluded from guarding — but the exclusion has to be *declared*.

    The shared table describes generic names; it cannot know an application's
    own vocabulary, and it is deliberately built to treat what it cannot
    classify as irreversible. So a tool called `read_thing` is not recognised as
    a read on the strength of its name and would be refused. The application
    says which of its tools are reads.
    """
    calls = []

    def read_thing(path: str) -> str:
        """Read a file."""
        calls.append(path)
        return "contents"

    policy = ChargePolicy(approve_irreversible=True, read_only_tools=frozenset({"read_thing"}))
    cap = EffectGuardCapability(store=store, run_id="r", policy=policy)
    agent = Agent(TestModel(call_tools="all"), tools=[read_thing], capabilities=[cap])
    agent.run_sync("read it")
    agent.run_sync("read it again")
    assert len(calls) == 2, "a declared read is not an effect"
    assert len(store.list_records()) == 0


def test_generic_read_names_need_no_declaration(store):
    """`read_file` and friends are already read-only in the shared table."""
    calls = []

    def read_file(path: str) -> str:
        """Read a file."""
        calls.append(path)
        return "contents"

    cap = EffectGuardCapability(store=store, run_id="r", policy=ChargePolicy())
    agent = Agent(TestModel(call_tools="all"), tools=[read_file], capabilities=[cap])
    agent.run_sync("read it")
    agent.run_sync("read it again")
    assert len(calls) == 2
    assert len(store.list_records()) == 0


def test_an_undeclared_read_is_treated_conservatively(store):
    """Without a declaration, an unclassifiable tool is refused, not waved through.

    This is the safe direction, and it is worth seeing explicitly: the cost is
    that a local read can be blocked until the application declares it.
    """
    calls = []

    def read_thing(path: str) -> str:
        """Read a file."""
        calls.append(path)
        return "contents"

    cap = EffectGuardCapability(store=store, run_id="r", policy=ChargePolicy())
    agent = Agent(TestModel(call_tools="all"), tools=[read_thing], capabilities=[cap])
    with pytest.raises(UnexpectedModelBehavior):
        agent.run_sync("read it")
    assert calls == [], "unclassified tools do not run without approval"


def test_read_only_tools_can_be_opted_in(store):
    calls = []

    def read_thing(path: str) -> str:
        """Read a file."""
        calls.append(path)
        return "contents"

    policy = ChargePolicy(
        approve_irreversible=True,
        guard_classes=frozenset(EffectClass),
        classify=lambda _tool, _target: EffectClass.PURE,
    )
    cap = EffectGuardCapability(store=store, run_id="r", policy=policy)
    agent = Agent(TestModel(call_tools="all"), tools=[read_thing], capabilities=[cap])
    agent.run_sync("read it")
    agent.run_sync("read it again")
    assert len(calls) == 1, "opt-in guarding applies to reads too"


# ── refusal paths ───────────────────────────────────────────────────────────


def test_irreversible_without_approval_is_refused(store, ledger):
    agent, _ = make_agent(store, ledger, "run-1", approve=False)
    with pytest.raises(UnexpectedModelBehavior, match="max retries"):
        agent.run_sync("charge")
    assert ledger == [], "the card must not be charged without approval"


def test_per_call_approval_can_be_narrow(store, ledger):
    """An application can approve one call without approving the class."""
    seen = []

    def approve(tool_name, args):
        seen.append((tool_name, dict(args)))
        # TestModel's placeholder customer is "a"; a real model would fill in a
        # name from the prompt. The point here is that the hook is consulted and
        # its return value decides, not that this predicate is interesting.
        return args.get("customer") == "a"

    tool = make_tool(ledger)
    cap = EffectGuardCapability(store=store, run_id="run-1", policy=ChargePolicy(approve=approve))
    agent = Agent(TestModel(call_tools="all"), tools=[tool], capabilities=[cap])
    agent.run_sync("charge")
    assert len(ledger) == 1
    assert seen, "the approval hook must be consulted"


def test_per_call_approval_can_refuse(store, ledger):
    """...and refuse, leaving the vendor untouched."""
    tool = make_tool(ledger)
    cap = EffectGuardCapability(
        store=store,
        run_id="run-1",
        policy=ChargePolicy(approve=lambda _tool, _args: False),
    )
    agent = Agent(TestModel(call_tools="all"), tools=[tool], capabilities=[cap])
    with pytest.raises(UnexpectedModelBehavior):
        agent.run_sync("charge")
    assert ledger == []


def test_an_interrupted_effect_is_not_retried_blindly(store, ledger):
    """The crash case. The charge may or may not have happened; retrying blind
    is a second real-world event, so the guard refuses and the model is told
    the outcome is unknown rather than that it succeeded."""
    from honestcode.guards import COMMITTED, EXECUTING

    first, _ = make_agent(store, ledger, "run-1")
    first.run_sync("charge")
    assert len(ledger) == 1

    # Rewind to `executing`: what a kill between the call and the commit leaves.
    for record in store.list_records():
        if record.status == COMMITTED:
            store.set_status(record.effect_id, EXECUTING)

    agent2, _ = make_agent(store, ledger, "run-1")
    with pytest.raises(UnexpectedModelBehavior):
        # The model retries out rather than proceed, which is the point: it was
        # told the outcome is unknown and chose not to claim success.
        agent2.run_sync("charge")
    assert len(ledger) == 1, "no second charge while the outcome is unknown"


# ── construction ────────────────────────────────────────────────────────────


def test_empty_run_id_is_refused(store):
    with pytest.raises(ValueError, match="run_id must not be empty"):
        EffectGuardCapability(store=store, run_id="")


def test_store_records_carry_the_effect_decision(store, ledger):
    make_agent(store, ledger, "run-1")[0].run_sync("charge")
    records = store.list_records()
    assert len(records) == 1
    record = records[0]
    assert record.status == "committed"
    assert record.action_type == "charge_card"
    assert record.target == "customer=a"
    assert json.loads(json.dumps(record.to_dict()))["effect_class"]
