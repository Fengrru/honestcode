# pydantic-ai integration

Durable idempotency for agent tool calls. An effect that already reached the
outside world is not carried out a second time.

```bash
pip install "honestcode[pydantic-ai]"
```

## Why this is not just `CallDeferred`

`pydantic-ai` already has deferral. From `before_tool_execute` you can raise
`CallDeferred` or `ApprovalRequired`, and the call is held until something
resolves it. That is the right tool for *a human should look at this*.

This integration answers a different question:

> Did the outside world already change, and do I no longer remember?

That question survives the process. In-memory deferral does not: the state lives
in the process that is about to die, and it is precisely after the process dies
that you need it. So the record has to be on disk before the tool runs.

| | `CallDeferred` / `ApprovalRequired` | this guard |
|---|---|---|
| Question | should this run? | did this already happen? |
| State lives in | the process | SQLite, on disk |
| Survives a crash | no | yes |
| Needs a run identifier | no | **yes** — see below |

## Usage

```python
from honestcode.guards import EffectStore
from honestcode.integrations.pydantic_ai import ChargePolicy, EffectGuardCapability

cap = EffectGuardCapability(
    store=EffectStore(".agent/effects.db"),
    run_id="order-4711-fulfilment",     # stable across retries
    policy=ChargePolicy(approve_irreversible=True),
)
agent = Agent(model, tools=[charge_card], capabilities=[cap])
```

### `run_id` is the field that matters

It is the durability boundary, and it must be the identifier the application can
reproduce after a restart — a workflow run id, a job id, an order id.

**Passing a fresh id on every attempt turns the guard into a very expensive
no-op**, because a new id makes every retry look like a new effect. This is not
a hypothetical: a key derived from the session id was the original design, and
it failed exactly this way, because a restart *is* a new session.

Two genuine orders must have two `run_id`s, or the guard will refuse the second
charge as a duplicate. Scope is authoritative: the guard does not guess what
counts as "the same".

### The effect classes

`guard_classes` defaults to `{REVERSIBLE, IRREVERSIBLE}` — reads are not
effects, and recording them would grow the store while adding nothing.
Classification goes through the shared action-name table in
`honestcode.guards.taxonomy`.

## What the model sees

When a call is deduplicated, the tool result the model receives is:

```
[effect-guard] charge_card(customer=alice) was NOT run again — this irreversible
effect already completed in run se_5a568811.... The previous result stands. If you
need a genuinely new effect, change the argument that identifies it.
```

A real model's response to this, in testing:

> The `vendor_charge` call for customer **alice** (4200 cents) was interrupted
> while executing, leaving its outcome **unknown** — the charge may or may not
> have already [gone through].

That is the point. The guard does not just prevent the duplicate; it stops the
agent from *claiming* success it cannot substantiate.

## Three things that will bite you

These are recorded because the first version of this integration got all three
wrong, and none of them were visible without running against a real model.

### 1. `args` arrives as a JSON string

`ToolCallPart.args` is annotated `dict` but is a **JSON string** while streaming,
which is the normal case for a real provider. Treating that as unkeyable made
the guard pass every real call through while reporting nothing: it looked
protected and was not. String args are parsed now, and a call that still cannot
be keyed is recorded in `cap.unguarded_calls` instead of failing silently.

Check `unguarded_calls` after a run — non-empty means those calls were not
protected.

### 2. Reads are not detected, they are declared

The shared table describes generic names (`read_file`, `grep`, `ls`, …). It
cannot know an application's own vocabulary, and it is deliberately built to
treat what it cannot classify as irreversible — so a tool called `search_docs`
is **refused** until the application says otherwise:

```python
policy = ChargePolicy(
    approve_irreversible=True,
    read_only_tools=frozenset({"search_docs", "list_index"}),   # declared reads
    # or, for anything needing more nuance:
    # classify=lambda tool, target: EffectClass.PURE if tool.startswith("get_") else ...,
)
```

Conservative by design. The cost is that you must say which tools are harmless —
and only you know whether `search_docs` hits a local index or a paid API.

### 3. A crash leaves the outcome *unknown*, not "did not happen"

If the process dies between the tool call and the commit, the record stays
`executing`. The next attempt refuses rather than retrying, because retrying an
irreversible effect whose outcome is unknown risks a second real-world event.

Recovering is a decision, not an automatic step. Someone checks the vendor:

```python
for decision in guard.recover(run_id):
    print(decision.action)   # 'query_external' for an irreversible effect
```

Then records what they found, and — if nothing landed — carries the effect
through under that answer:

```python
guard.resolve(effect_id, COMPENSATED)              # operator: nothing went through
outcome = await guard.recovered_retry(effect_id, execute=charge_card)
print(outcome.executed)                             # True: the charge ran, once
```

`recovered_retry` refuses the two cases that must never be papered over: a
`committed` effect is served from its receipt rather than re-run, and an
`executing` one raises, because an unresolved outcome is exactly what must not
be retried blind.

## Verified behaviour

Against `pydantic-ai` 2.55.0, `TestModel` for the mechanics and DeepSeek for the
end-to-end run:

| behaviour | result |
|---|---|
| `handler()` not called → tool does not run | verified |
| same `run_id`, second agent, same store | one charge, receipt returned |
| different `run_id` | two charges (two genuine orders) |
| irreversible without approval | refused, vendor untouched |
| crash mid-call, then resume | one charge; outcome reported unknown |
| operator resolves, `recovered_retry` | the charge runs, once |
| string-form `args` | keyed, not waved through |
| declared read | not guarded |

`tests/test_pydantic_ai_integration.py` covers the capability without a network
call, and skips when `pydantic-ai` is absent. The scope and retry mechanics are
covered by `tests/test_guards.py`.

## A packaging note

`honestcode` pins `mcp>=1.6.0,<2.0`. `pydantic-ai-slim` depends on `mcp` with no
upper bound, so installing both into a fresh environment used to resolve `mcp` to
2.x — where `FastMCP` was renamed — and the import of `honestcode`'s own MCP
server failed. The `pydantic-ai` extra pins `mcp<2` alongside so it cannot break
the core package it is installed next to.
