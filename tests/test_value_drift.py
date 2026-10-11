"""Tests for the value-drift benchmark and the guard it documents.

These run on every test pass so the benchmark cannot silently rot: the corpus
invariants are asserted, the ported matcher is pinned to its documented
behaviour, and the guard's accept/reject semantics are stated as executable
facts rather than prose.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "value_drift"
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from corpus import SCENARIOS  # noqa: E402
from run import (  # noqa: E402
    CORRECT,
    NO_MATCH,
    SILENT_DRIFT,
    WRONG_SPAN,
    classify,
    run_suite,
    verify_corpus,
)
from strategies import (  # noqa: E402
    GUARDED_STRATEGIES,
    UNGUARDED_STRATEGIES,
    extract_value_tokens,
    lev,
    match_at_all,
    preserves_value_tokens,
)

# ── corpus integrity ────────────────────────────────────────────────────────


def test_corpus_is_well_formed():
    assert verify_corpus() == []


def test_corpus_is_not_empty():
    assert len(SCENARIOS) >= 8


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_scenario_is_admissible(scenario):
    """The intended region must carry the caller's values, else unsolvable."""
    assert preserves_value_tokens(scenario.intended_region, scenario.search)
    # The competing region must differ in value, or there is nothing to guard.
    assert not preserves_value_tokens(scenario.competing_region, scenario.search)
    # The edit must be a genuine value change.
    assert not preserves_value_tokens(scenario.replace, scenario.search)


# ── the guard's contract ────────────────────────────────────────────────────


def test_extract_value_tokens_finds_numeric_and_string_literals():
    tokens = extract_value_tokens('port = 3000; name = "server"; f = 1.5; h = 0xFF')
    assert tokens == sorted(["3000", '"server"', "1.5", "0xFF"])


def test_extract_value_tokens_ignores_identifiers():
    assert extract_value_tokens("def handler(request): return response") == []


def test_guard_accepts_same_values_different_formatting():
    """Whitespace drift is allowed; the values are what matter."""
    assert preserves_value_tokens("    port=3000,  ", "port=3000,")


def test_guard_rejects_a_different_number():
    """The failure this guard exists for."""
    assert not preserves_value_tokens("port=9999,", "port=3000,")


def test_guard_rejects_a_prefix_number():
    """`30000` must not satisfy a search for `3000`."""
    assert not preserves_value_tokens("timeout=30000,", "timeout=3000,")


def test_guard_rejects_an_extra_literal():
    """A region carrying a value the caller never named is refused."""
    assert not preserves_value_tokens(
        '{"host": "h", "port": 8880, "production": {}}',
        '{"host": "h", "port": 8880}',
    )


def test_guard_rejects_a_missing_literal():
    assert not preserves_value_tokens(
        '"host": "h",\n"port": 8880,\n}',
        '"production": {\n"host": "h",\n"port": 8880,\n}',
    )


def test_guard_rejects_a_different_string_literal():
    assert not preserves_value_tokens(
        'cur.execute("SELECT id FROM users_active")',
        'cur.execute("SELECT id FROM users_archived")',
    )


# ── the ported matcher ──────────────────────────────────────────────────────


def test_levenshtein_distance_matches_known_values():
    assert lev("", "") == 0
    assert lev("abc", "") == 3
    assert lev("kitten", "sitting") == 3
    assert lev("3000", "30000") == 1


def test_exact_strategy_needs_a_verbatim_occurrence():
    from strategies import strategy_exact

    assert strategy_exact("port=3000", "port=3000") is not None
    assert strategy_exact("port=3000", "port=9999") is None


# ── end-to-end behaviour ────────────────────────────────────────────────────


def test_guarded_matcher_never_drifts():
    """The claim this benchmark exists to check."""
    outcomes = run_suite(GUARDED_STRATEGIES)
    drifted = [o for o in outcomes if o.drifted]
    assert drifted == [], f"silent drift on: {[o.scenario.id for o in drifted]}"


def test_guarded_matcher_lands_on_the_caller_named_region():
    outcomes = run_suite(GUARDED_STRATEGIES)
    wrong = [o for o in outcomes if o.result not in (CORRECT, NO_MATCH)]
    assert wrong == [], f"misapplied edits: {[(o.scenario.id, o.result) for o in wrong]}"


def test_unguarded_cascade_behaviour_is_recorded():
    """Pin the current finding.

    The guard changes no outcome on this corpus, because a region the guard
    accepts necessarily carries the search string's values. If a future corpus
    or strategy change makes the guard load-bearing, this test fails and the
    README's conclusion has to be rewritten rather than quietly invalidated.
    """
    guarded = {o.scenario.id: o.result for o in run_suite(GUARDED_STRATEGIES)}
    unguarded = {o.scenario.id: o.result for o in run_suite(UNGUARDED_STRATEGIES)}
    assert guarded == unguarded, (
        "the guard now changes outcomes — update benchmarks/value_drift/README.md's conclusion"
    )


def test_classify_calls_a_wrong_region_silent_drift():
    """A region the caller never named was rewritten, so it is drift."""
    scenario = SCENARIOS[0]
    patched = scenario.file.replace(scenario.distractor, scenario.replace, 1)
    index = scenario.file.index(scenario.distractor)
    result = classify(scenario, patched, index, len(scenario.distractor), scenario.distractor)
    assert result == SILENT_DRIFT


def test_classify_calls_an_untouched_file_no_match():
    scenario = SCENARIOS[0]
    assert classify(scenario, scenario.file, 0, 0, "") == NO_MATCH


def test_classify_rejects_a_span_that_matches_neither_outcome():
    scenario = SCENARIOS[0]
    patched = scenario.file.replace("port: int = 8000", "port: int = 1234", 1)
    index = scenario.file.index("port: int = 8000")
    result = classify(scenario, patched, index, len("port: int = 8000"), "port: int = 8000")
    assert result == WRONG_SPAN


def test_every_scenario_resolves_under_the_guarded_cascade():
    """No scenario should be unsolvable: the corpus asserts it is admissible."""
    outcomes = run_suite(GUARDED_STRATEGIES)
    unresolved = [o.scenario.id for o in outcomes if o.result == NO_MATCH]
    assert unresolved == [], f"guarded cascade could not resolve: {unresolved}"


def test_match_at_all_reports_the_winning_strategy():
    scenario = SCENARIOS[0]
    match, name = match_at_all(GUARDED_STRATEGIES, scenario.file, scenario.search)
    assert match is not None
    assert name in {s.name for s in GUARDED_STRATEGIES}
