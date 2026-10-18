#!/usr/bin/env python3
"""Does formatting-tolerant matching silently change the wrong value?

The question this benchmark answers, for three matcher families:

    An agent asks to edit a value. Its search string does not identify a unique
    region — a near-miss sibling differs only in a value literal. Where does the
    edit land, and is the caller told the truth?

Verdict per scenario
--------------------

``correct``         the region the caller named was the region rewritten
``silent_drift``    a sibling region was rewritten instead, and the call
                    reported success — a value the caller never named is gone
``no_match``        nothing changed (fail-safe, recoverable)
``wrong_span``      the file changed but matches neither of the above

The headline number for each matcher is its ``silent_drift`` count: edits that
applied cleanly to the wrong value. Nothing at the call site distinguishes that
from success, so it cannot be recovered from without reading the file.

Usage::

    python benchmarks/value_drift/run.py
    python benchmarks/value_drift/run.py --json
    python benchmarks/value_drift/run.py --verify     # corpus self-check only

Exit code 2 if the guarded matcher drifts, or if the corpus is inconsistent —
the same contract the other benchmarks use to gate CI.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus import SCENARIOS, Scenario  # noqa: E402
from strategies import (  # noqa: E402
    ALL_STRATEGY_SETS,
    Strategy,
    match_at_all,
    preserves_value_tokens,
)

CORRECT = "correct"
SILENT_DRIFT = "silent_drift"
NO_MATCH = "no_match"
WRONG_SPAN = "wrong_span"

_ICON = {CORRECT: "ok   ", SILENT_DRIFT: "DRIFT", NO_MATCH: "skip ", WRONG_SPAN: "bad  "}

GUARDED_LABEL = "guarded (fuzzy-patch)"


@dataclass(frozen=True)
class Outcome:
    scenario: Scenario
    strategy: str
    result: str
    matched_text: str = ""

    @property
    def drifted(self) -> bool:
        return self.result == SILENT_DRIFT


# ── corpus integrity ────────────────────────────────────────────────────────


def verify_corpus() -> list[str]:
    """Return a list of problems; empty means the corpus is well-formed."""
    problems: list[str] = []
    for s in SCENARIOS:
        for label, text in (("target", s.target), ("distractor", s.distractor)):
            if s.file.count(text) != 1:
                problems.append(f"{s.id}: {label} appears {s.file.count(text)}x (must be 1)")
        if s.target == s.distractor:
            problems.append(f"{s.id}: target and distractor are identical")
        if s.intended_region not in (s.target, s.distractor):
            problems.append(f"{s.id}: intended_region is neither target nor distractor")
        if s.replace == s.search:
            problems.append(f"{s.id}: replace equals search")
        if s.expected_after not in s.replace:
            problems.append(f"{s.id}: expected_after is not in replace")
        if s.forbidden_after in s.replace:
            problems.append(f"{s.id}: forbidden_after is in replace")
        if s.forbidden_after in s.intended_region:
            problems.append(f"{s.id}: forbidden_after is inside the intended region")
        # The intended region must carry the values the caller named, otherwise
        # the scenario is not solvable and proves nothing about matching.
        if not preserves_value_tokens(s.intended_region, s.search):
            problems.append(
                f"{s.id}: the intended region does not preserve the search's values, "
                "so no correct matcher could resolve it as intended"
            )
    return problems


# ── scoring ─────────────────────────────────────────────────────────────────


def classify(
    scenario: Scenario, patched: str, match_index: int, match_length: int, matched_text: str
) -> str:
    """Judge where the edit landed by *span*, not by substring containment.

    The previous implementation rebuilt the expected file from the same
    ``match_index``/``match_length`` it was judging, so ``patched == expected``
    was a tautology whenever the matched text was a substring of the intended
    region — a one-character interior match was scored ``correct``. Here the
    intended span is located independently and the *result* is judged: the
    match must lie inside the intended region, the resulting region must carry
    the replacement's values and not the distractor's, the distractor must
    remain intact elsewhere in the file, and the file must still parse.
    """
    if not matched_text:
        return NO_MATCH if patched == scenario.file else WRONG_SPAN

    intended_start = scenario.file.index(scenario.intended_region)
    intended_end = intended_start + len(scenario.intended_region)
    if intended_start <= match_index and match_index + match_length <= intended_end:
        if patched == scenario.file:
            return NO_MATCH
        # The edit landed somewhere inside the region the caller named. Judge
        # the *resulting region*, not the whole file: ``forbidden_after`` is
        # the distractor's value and legitimately remains elsewhere in the
        # file. The replacement must have taken effect here, and splicing it
        # must leave valid code — a fragment match that inserts a whole block
        # into the middle of the region fails the parse check instead of being
        # scored correct merely because its span was "contained".
        prefix = scenario.intended_region[: match_index - intended_start]
        suffix = scenario.intended_region[match_index - intended_start + match_length :]
        patched_region = prefix + scenario.replace + suffix
        if scenario.expected_after not in patched_region:
            return WRONG_SPAN
        if scenario.forbidden_after in patched_region:
            return WRONG_SPAN
        if scenario.competing_region not in patched:
            return WRONG_SPAN
        try:
            ast.parse(patched)
        except SyntaxError:
            return WRONG_SPAN
        return CORRECT

    competing_start = scenario.file.index(scenario.competing_region)
    competing_end = competing_start + len(scenario.competing_region)
    if competing_start <= match_index and match_index + match_length <= competing_end:
        return SILENT_DRIFT
    if patched == scenario.file:
        return NO_MATCH
    return WRONG_SPAN


def run_scenario(scenario: Scenario, strategies: list[Strategy]) -> Outcome:
    match, name = match_at_all(strategies, scenario.file, scenario.search)
    if match is None:
        return Outcome(scenario, "none", NO_MATCH)
    matched_text = scenario.file[match.index : match.end]
    patched = scenario.file[: match.index] + scenario.replace + scenario.file[match.end :]
    result = classify(scenario, patched, match.index, match.length, matched_text)
    return Outcome(scenario, name or "none", result, matched_text)


def run_suite(strategies: list[Strategy]) -> list[Outcome]:
    return [run_scenario(s, strategies) for s in SCENARIOS]


# ── reporting ───────────────────────────────────────────────────────────────


def print_report(sets: dict[str, list[Outcome]]) -> None:
    width = max(len(s.id) for s in SCENARIOS) + 2

    for label, outcomes in sets.items():
        drift = sum(o.drifted for o in outcomes)
        correct = sum(o.result == CORRECT for o in outcomes)
        header = f"{label}  —  correct {correct}/{len(outcomes)}, silent drift {drift}"
        print()
        print(header)
        print("─" * min(len(header) + 6, 92))
        for o in outcomes:
            via = f"  via {o.strategy}" if o.result in (CORRECT, SILENT_DRIFT) else ""
            print(f"  {_ICON[o.result]} {o.scenario.id:<{width}} {o.result}{via}")

    print()
    print("═" * 88)
    print("SUMMARY — how often the edit landed on a region the caller never named")
    print("═" * 88)
    print(f"{'matcher':<46}{'correct':>9}{'drift':>8}{'no match':>11}{'bad':>6}")
    print("─" * 80)
    for label, outcomes in sets.items():
        drift = sum(o.drifted for o in outcomes)
        correct = sum(o.result == CORRECT for o in outcomes)
        skipped = sum(o.result == NO_MATCH for o in outcomes)
        bad = sum(o.result == WRONG_SPAN for o in outcomes)
        print(f"{label:<46}{correct:>9}{drift:>8}{skipped:>11}{bad:>6}")

    print()
    drifted = [(label, o) for label, outcomes in sets.items() for o in outcomes if o.drifted]
    refused = [
        (label, o)
        for label, outcomes in sets.items()
        for o in outcomes
        if o.result == NO_MATCH and label != GUARDED_LABEL
    ]
    if drifted:
        print("Silent drift — a value the caller never named was overwritten:")
        for label, o in drifted:
            print(f"  · {o.scenario.id:<28} [{label}]  via {o.strategy}")
            print(f"      {o.scenario.what}")
    else:
        print(
            "No silent drift: every matcher either edited the region the caller named or changed nothing."
        )

    if refused:
        print()
        print(f"Fail-safe refusals ({len(refused)}) — edit not applied, caller can retry:")
        for label, o in refused:
            print(f"  · {o.scenario.id:<28} [{label}]")

    # Isolate what the value guard itself contributes, by running the same
    # eight strategies with and without it.
    print()
    print("─" * 88)
    print("Does the value guard change any outcome?")
    print("─" * 88)
    guarded_by_id = {o.scenario.id: o for o in sets[GUARDED_LABEL]}
    changed = [
        (o.scenario.id, o.result, guarded_by_id[o.scenario.id].result)
        for o in sets["unguarded (8 strategies, no value guard)"]
        if o.scenario.id in guarded_by_id and o.result != guarded_by_id[o.scenario.id].result
    ]
    if changed:
        for sid, un, g in changed:
            print(f"  · {sid}: unguarded={un} → guarded={g}")
    else:
        print("  No scenario distinguishes the guard on this corpus: every intended")
        print("  region carries exactly the values the caller named, and every competing")
        print("  region carries different ones, so both cascades land on the intended")
        print("  region. The guard is a safety net for the case where a search string's")
        print("  values match no region at all — see benchmarks/value_drift/README.md.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    ap.add_argument("--verify", action="store_true", help="check corpus integrity and exit")
    args = ap.parse_args()

    problems = verify_corpus()
    if problems:
        print("corpus problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    if args.verify:
        print(f"corpus ok: {len(SCENARIOS)} scenarios, all well-formed")
        return 0

    sets = {label: run_suite(strategies) for label, strategies in ALL_STRATEGY_SETS.items()}

    if args.json:
        payload = {
            label: [
                {
                    "scenario": o.scenario.id,
                    "strategy": o.strategy,
                    "result": o.result,
                    "matched": o.matched_text[:70],
                }
                for o in outcomes
            ]
            for label, outcomes in sets.items()
        }
        print(json.dumps(payload, indent=2))
    else:
        print_report(sets)

    guarded = sets[GUARDED_LABEL]
    # Every scenario must land on the region the caller named. Refusing all
    # edits (``no_match``) used to pass this gate, which made an over-strict
    # matcher — the recall regression this benchmark exists to catch — green.
    failed = [o for o in guarded if o.result != CORRECT]
    status_stream = sys.stderr if args.json else sys.stdout
    if failed:
        print(
            f"\nFAIL: the guarded matcher did not produce the intended edit on "
            f"{len(failed)} scenario(s)",
            file=status_stream,
        )
        return 2
    print(
        f"\nOK: guarded matcher applied the intended edit on all {len(guarded)} scenarios",
        file=status_stream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
