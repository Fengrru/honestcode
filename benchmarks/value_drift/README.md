# value_drift — does formatting-tolerant matching silently change the wrong value?

A benchmark for one question:

> An agent asks to edit a value. Its search string does not pin down a unique
> region — a near-miss sibling differs only in a value literal. Where does the
> edit land, and is the caller told the truth?

This exists because the 2026 edit-application ecosystem converged on guards that
protect **where** a match lands (raised similarity floors, ambiguity = hard
failure, disproportionate-span rejection). None of them has an opinion about
**which values** the matched region carries. `fuzzy-patch` adds that dimension,
and this benchmark was written to find out whether it earns its place.

```bash
python benchmarks/value_drift/run.py            # full report
python benchmarks/value_drift/run.py --json     # machine-readable
python benchmarks/value_drift/run.py --verify   # corpus self-check
```

Exit code is 2 if the guarded matcher drifts outside the region the caller
named, or if the corpus is inconsistent — the same contract the other
benchmarks use to gate CI.

## Method

`corpus.py` holds ten fake episodes. Each has a `target` (the region the agent
means to edit), a `distractor` (a near-miss twin stating a different value), a
`search` string that is *not* a verbatim copy of the target, and a `replace`
string. Two failure modes are distinguished, and they are not equally bad:

| outcome | meaning |
|---|---|
| `correct` | the region the caller named was the region rewritten |
| `silent_drift` | a sibling region was rewritten instead, **and the call reported success** |
| `no_match` | nothing changed — fail-safe, the agent can re-read and retry |
| `wrong_span` | the file changed but matches neither of the above |

`silent_drift` is the failure that matters: nothing at the call site
distinguishes it from a correct edit, so it cannot be recovered from without
reading the file afterwards.

`strategies.py` carries a faithful Python port of all eight `fuzzy-patch`
strategies **including** its value guard, the same eight with the guard removed,
and a matcher in the shape the ecosystem converged on (similarity floor +
uniqueness + disproportionate-span rejection). The guarded and unguarded sets
differ in exactly one thing, so any difference in outcome is attributable to the
guard.

Two corpus invariants make the results meaningful, and `--verify` enforces them:

1. The **intended region must carry the values the caller named**. If it does
   not, no correct matcher could resolve the search to that region and the
   scenario would prove nothing.
2. `expected_after` / `forbidden_after` describe the two possible file states,
   so the report can state precisely which one occurred.

## Result

```
matcher                                    correct   drift   no match   bad
guarded (fuzzy-patch)                           10       0          0     0
unguarded (8 strategies, no value guard)        10       0          0     0
competitor: similarity floor + uniqueness        1       0          9     0
```

Standing up a formatting-tolerant matcher that resolves every one of these edits
correctly is not trivial — the crude similarity matcher refuses nine of the ten,
and a refusal is what a conservative matcher does to avoid drift.

**But the value guard is not what carries the result.** The guarded and
unguarded cascades score identically. Reverse-engineering why produced the more
useful finding:

> A region accepted by the guard necessarily carries exactly the literal
> multiset of the search string. So whenever the search string carries the value
> being replaced — which is what an agent naturally writes, and what all ten
> scenarios do — *any region the guard accepts already has the caller's values*.
> The guard cannot discriminate, because on such a search string its accept and
> reject sets are both "regions whose values equal the caller's".

The guard's real effect is on **recall**, not precision. It is what refuses a
match when the search string's literals match no region at all, converting a
would-be drift into a `no_match` — the correct answer for this guard, but a
constraint on what the guard can prove. Evidence for this came out of an earlier
draft of this corpus: several scenarios originally quoted a nested block without
its structural keys, e.g. omitting `"production"` and `"timeout_ms"` from the
search. In all of them the guard rejected **the intended region itself**,
because the literal counts differed (`benchmarks/value_drift/corpus.py`, the
`env_port_nested` history). That is the guard behaving exactly as designed, and
it is also why this benchmark cannot exhibit a `silent_drift` case for it.

## What this means for the integration decision

The honest reading, and the reason this file is short on adjectives:

* **The verdict on `fuzzy-patch` as a whole is positive** — with ten
  admissibly-ambiguous edits, it lands on the caller's region every time, while
  a conservative competitor refuses nine of ten. The dual-anchor design
  (`findInOriginalViaAnchors`) is doing the work here, not the guard.
* **The verdict on the value guard specifically is "unproven, not
  disproven".** On this corpus it changes zero outcomes. Claiming a differentiated
  safety property from it would overstate the evidence; recommending it as
  defence-in-depth is fair, because it is free on the passing cases and it does
  fire (ten times across the strategy/region matrix in an instrumented run).
* **The differentiating failure mode is wrong-region matching, not value
  drift.** That is what the competitor's nine refusals are protecting against,
  and it is where any future work should aim its evidence.

To actually demonstrate the guard, the corpus would need a case where the
distractor is reachable by an *earlier* strategy than the target **and** carries
different values **and** the intended region still preserves the search's values
— which by the argument above cannot exist while the search string carries the
value being replaced. The remaining honest option is a larger, real-world corpus
where search strings are not hand-authored to be solvable, and the measured
quantity is how often the guard's refusals turn a would-be drift into a safe
`no_match`.

## Relationship to the TypeScript suite

The regression now lives where the code it tests lives: the same ten episodes
were ported to `awesome-agent-infra/packages/fuzzy-patch/__tests__/` as Bun
tests, alongside a self-test that proves their detector can tell a correct edit
from a drifting one. That copy runs with the package's own suite; this one runs
with honestcode's.

What this side keeps and the other does not is the **comparison**.
`strategies.py` carries a conservative competitor matcher (similarity floor +
uniqueness), which is the shape the 2026 edit-application ecosystem converged on.
It refused nine of the ten, and reproducing that is the reason to keep two copies
rather than one.

If the episodes change, change both. The `SCENARIOS` table is the same data and
the two are meant to stay in step.

## Provenance

`strategies.py` is a port of `packages/fuzzy-patch/src/index.ts` from
`awesome-agent-infra` (the guard lives at lines 100–136 of that file). The port
is intended to be behaviour-preserving, including the values-normalisation used
by `preservesValueTokens`; if the upstream implementation changes, this port has
to change with it or the comparison stops meaning anything.
