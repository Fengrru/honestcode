"""Tests for the side-effect guard.

Two things are being tested here, and they are different in kind.

**Behaviour** — that an effect runs at most once, that the key survives a
restart, that a crash between "called" and "recorded" is visible rather than
invisible, and that preconditions actually refuse.

**The three upstream defects** — each test that pins a fix records the upstream
behaviour in its docstring, because the point of this package is that these
bugs are not carried into the port. If someone later "simplifies" the code back
toward the original shape, these should fail.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
from pathlib import Path

import pytest

from honestcode.guards import (
    COMMITTED,
    COMPENSATED,
    EXECUTING,
    FAILED,
    PENDING,
    EffectClass,
    EffectNotRunnable,
    EffectStore,
    GuardError,
    PreconditionFailed,
    RecoveryStrategy,
    SideEffectGuard,
    classify,
    durable_scope,
    precondition,
    strategy_for,
)

# ── isolation ───────────────────────────────────────────────────────────────


def _imported_modules(path: Path) -> list[str]:
    """Every absolute module name imported by a file, relative imports skipped."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def _outside_package_imports(path: Path) -> list[str]:
    """Violations of the isolation rule, as human-readable strings.

    Two rules, both checked on the parsed module name rather than on the source
    text: nothing outside the standard library, and any ``honestcode.*`` import
    must be ``honestcode.guards.*``.
    """
    violations: list[str] = []
    for module in _imported_modules(path):
        root = module.split(".")[0]
        if root == "honestcode":
            if not module.startswith("honestcode.guards"):
                violations.append(f"{path.name} imports {module}")
            continue
        if root in _STDLIB or root in _THIS_PACKAGE:
            continue
        violations.append(f"{path.name} imports {module}")
    return violations


_THIS_PACKAGE = {"guards", "taxonomy", "store", "guard"}
_STDLIB = {
    "__future__",
    "collections",
    "contextlib",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "sqlite3",
    "threading",
    "time",
    "typing",
    "uuid",
}


def test_guards_import_only_stdlib_and_itself():
    """The isolation rule that keeps this package extractable.

    ``honestcode.guards`` is expected to move to its own distribution. That only
    works if it never reaches into the rest of HonestCode — the rest of
    HonestCode answers questions about code, this package wraps code that
    changes the world.
    """
    guards_dir = Path(__file__).resolve().parents[1] / "honestcode" / "guards"
    offenders = [
        violation
        for path in sorted(guards_dir.glob("*.py"))
        for violation in _outside_package_imports(path)
    ]
    assert offenders == [], f"guards must stay stdlib-only: {offenders}"


def test_the_isolation_check_actually_fails_on_a_violation(tmp_path):
    """A checker that cannot fail is not a checker.

    The first version of the rule above looked for the *string*
    ``honestcode.guards`` in the source, so any file that also imported a
    sibling passed regardless of what else it imported. Pinning the checker
    against a planted violation is what makes the rule above meaningful.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from honestcode.verify.verify import verify_tree\n",
        encoding="utf-8",
    )
    assert _outside_package_imports(planted) == ["planted.py imports honestcode.verify.verify"]


def test_the_isolation_check_allows_its_own_package(tmp_path):
    allowed = tmp_path / "allowed.py"
    allowed.write_text(
        "from honestcode.guards.taxonomy import EffectClass\nimport json\n",
        encoding="utf-8",
    )
    assert _outside_package_imports(allowed) == []


def test_guards_do_not_reference_honestcode_protocols():
    """No findings, no reports — a guard returns receipts, not verdicts."""
    guards_dir = Path(__file__).resolve().parents[1] / "honestcode" / "guards"
    forbidden = {"Finding", "build_report", "render_text", "verify"}
    hits: list[str] = []
    for path in sorted(guards_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                hits.append(f"{path.name}: {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                hits.append(f"{path.name}: .{node.attr}")
    assert hits == [], f"guards must not use HonestCode's verification protocol: {hits}"


# ── taxonomy ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("read_file", EffectClass.PURE),
        ("grep", EffectClass.PURE),
        ("write_file", EffectClass.IDEMPOTENT),
        ("upsert", EffectClass.IDEMPOTENT),
        ("edit_file", EffectClass.REVERSIBLE),
        ("patch", EffectClass.REVERSIBLE),
        ("send_email", EffectClass.IRREVERSIBLE),
        ("git_push", EffectClass.IRREVERSIBLE),
        ("deploy", EffectClass.IRREVERSIBLE),
        ("charge", EffectClass.IRREVERSIBLE),
    ],
)
def test_classify_known_actions(action, expected):
    assert classify(action) is expected


def test_classify_is_case_and_separator_tolerant():
    assert classify("Read_File") is EffectClass.PURE
    assert classify("git-push") is EffectClass.IRREVERSIBLE
    assert classify("  deploy  ") is EffectClass.IRREVERSIBLE


def test_unknown_action_defaults_to_irreversible():
    """Upstream defaulted unknown actions to *idempotent*, which made an
    unrecognised action retryable. The safe direction is the opposite one."""
    assert classify("frobnicate_widget") is EffectClass.IRREVERSIBLE


def test_unknown_action_uses_word_bounded_target_hints():
    """Upstream used ``target.contains("push")``, so a path merely containing the
    letters classified as irreversible. Word boundaries fix that. Note the
    consequence: when neither the name nor the target yields a hint, the answer
    is still irreversible — bounded matching sharpens the hint check, it does
    not make the default permissive."""
    # An email target is a real hint.
    assert classify("mystery", "notify-admin@example.com") is EffectClass.IRREVERSIBLE
    # A push that is only a substring of a path component is not a hint, so the
    # decision falls through to the safe default rather than to "idempotent".
    assert classify("mystery", "/srv/pushkin/report.txt") is EffectClass.IRREVERSIBLE
    # The distinction that matters: the path must not be treated as an
    # idempotent write just because it looks like a file.
    assert classify("mystery", "/srv/pushkin/report.txt") is not EffectClass.IDEMPOTENT


def test_effect_class_orders_by_severity():
    assert EffectClass.PURE < EffectClass.IDEMPOTENT < EffectClass.REVERSIBLE
    assert EffectClass.REVERSIBLE < EffectClass.IRREVERSIBLE
    assert EffectClass.IRREVERSIBLE.severity > EffectClass.PURE.severity


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (EffectClass.PURE, RecoveryStrategy.REPLAY),
        (EffectClass.IDEMPOTENT, RecoveryStrategy.REPLAY),
        (EffectClass.REVERSIBLE, RecoveryStrategy.COMPENSATE),
        (EffectClass.IRREVERSIBLE, RecoveryStrategy.QUERY_AND_CONFIRM),
    ],
)
def test_recovery_strategy_table(cls, expected):
    assert strategy_for(cls) is expected


# ── the store ───────────────────────────────────────────────────────────────


@pytest.fixture
def store():
    s = EffectStore(":memory:")
    yield s
    s.close()


def test_store_rejects_a_duplicate_key(store):
    kwargs = {
        "session_id": "s1",
        "idempotency_key": "k1",
        "action_type": "write_file",
        "target": "a.py",
        "effect_class": EffectClass.IDEMPOTENT,
        "payload_hash": "h",
        "preconditions": (),
    }
    first = store.insert(effect_id="se_1", **kwargs)
    second = store.insert(effect_id="se_2", **kwargs)
    assert first is not None
    assert second is None, "the database, not a Python check, must enforce uniqueness"
    assert store.find_by_key("s1", "k1").effect_id == "se_1"


def test_store_scopes_keys_per_session(store):
    common = {
        "idempotency_key": "k1",
        "action_type": "write_file",
        "target": "a.py",
        "effect_class": EffectClass.IDEMPOTENT,
        "payload_hash": "h",
        "preconditions": (),
    }
    assert store.insert(effect_id="se_1", session_id="s1", **common) is not None
    assert store.insert(effect_id="se_2", session_id="s2", **common) is not None


def test_store_round_trips_every_field(store):
    store.insert(
        effect_id="se_1",
        session_id="s1",
        idempotency_key="k1",
        action_type="edit_file",
        target="a.py",
        effect_class=EffectClass.REVERSIBLE,
        payload_hash="h",
        preconditions=("budget", "clean_tree"),
    )
    record = store.get("se_1")
    assert record is not None
    assert record.effect_class is EffectClass.REVERSIBLE
    assert record.preconditions == ("budget", "clean_tree")
    assert record.status == PENDING
    assert record.committed_at is None


def test_store_unknown_status_is_refused(store):
    store.insert(
        effect_id="se_1",
        session_id="s1",
        idempotency_key="k1",
        action_type="write_file",
        target="a.py",
        effect_class=EffectClass.IDEMPOTENT,
        payload_hash="h",
        preconditions=(),
    )
    with pytest.raises(Exception, match="unknown status"):
        store.set_status("se_1", "not_a_status")


def test_store_set_status_on_missing_effect_raises(store):
    with pytest.raises(Exception, match="no such effect"):
        store.set_status("nope", FAILED)


def test_unresolved_lists_pending_and_executing(store):
    for i, status in enumerate([PENDING, EXECUTING, COMMITTED, FAILED, COMPENSATED]):
        store.insert(
            effect_id=f"se_{i}",
            session_id="s1",
            idempotency_key=f"k{i}",
            action_type="write_file",
            target="a.py",
            effect_class=EffectClass.IDEMPOTENT,
            payload_hash="h",
            preconditions=(),
            status=status,
        )
    ids = {r.effect_id for r in store.unresolved("s1")}
    assert ids == {"se_0", "se_1"}, "only unknown-outcome records are recovery work"


def test_store_persists_to_a_file_and_reopens(tmp_path):
    db = tmp_path / "nested" / "effects.db"
    first = EffectStore(db)
    first.insert(
        effect_id="se_1",
        session_id="s1",
        idempotency_key="k1",
        action_type="charge",
        target="cus_1",
        effect_class=EffectClass.IRREVERSIBLE,
        payload_hash="h",
        preconditions=(),
    )
    first.close()

    second = EffectStore(db)
    record = second.find_by_key("s1", "k1")
    second.close()
    assert record is not None
    assert record.effect_class is EffectClass.IRREVERSIBLE


def test_store_creates_missing_parent_directories(tmp_path):
    db = tmp_path / "a" / "b" / "c" / "effects.db"
    store = EffectStore(db)
    store.close()
    assert db.exists()


# ── defect 1: the idempotency key is real and durable ───────────────────────


def test_second_call_is_deduplicated(tmp_path):
    db = tmp_path / "effects.db"
    calls: list[int] = []

    def charge():
        calls.append(1)
        return {"status": "ok"}

    guard = SideEffectGuard(session_id="s1", store_path=str(db), approve_irreversible=True)
    first = guard.run("charge", target="cus_1", payload={"cents": 100}, execute=charge)
    second = guard.run("charge", target="cus_1", payload={"cents": 100}, execute=charge)
    guard.close()

    assert first.executed is True
    assert second.executed is False
    assert second.deduplicated is True
    assert len(calls) == 1, "the effect must reach the outside world once"
    assert first.effect_id == second.effect_id


def test_idempotency_survives_a_process_restart(tmp_path):
    """The upstream defect: ``let _idempotency_key = ...`` discarded the key and
    records lived in a ``Vec``. A restart therefore lost every key and the agent
    re-ran the effect — the exact situation idempotency exists for."""
    db = tmp_path / "effects.db"
    calls: list[int] = []

    def send():
        calls.append(1)
        return "sent"

    first_guard = SideEffectGuard(session_id="s1", store_path=str(db), approve_irreversible=True)
    first_guard.run("send_email", target="a@b.com", payload={"body": "hi"}, execute=send)
    first_guard.close()

    # A new process, a new guard, same database.
    second_guard = SideEffectGuard(session_id="s1", store_path=str(db), approve_irreversible=True)
    outcome = second_guard.run("send_email", target="a@b.com", payload={"body": "hi"}, execute=send)
    second_guard.close()

    assert outcome.executed is False
    assert len(calls) == 1, "the email must not be sent twice across a restart"


def test_different_payloads_are_different_effects(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    first = guard.run(
        "charge",
        target="cus_1",
        payload={"cents": 100},
        execute=lambda: "a",
        approve_irreversible=True,
    )
    second = guard.run(
        "charge",
        target="cus_1",
        payload={"cents": 200},
        execute=lambda: "b",
        approve_irreversible=True,
    )
    third = guard.run(
        "charge",
        target="cus_2",
        payload={"cents": 100},
        execute=lambda: "c",
        approve_irreversible=True,
    )
    guard.close()
    assert len({first.effect_id, second.effect_id, third.effect_id}) == 3


def test_payload_hashing_is_order_independent(tmp_path):
    """A dict that serialises in a different key order is the same effect."""
    guard = SideEffectGuard(
        session_id="s1", store_path=str(tmp_path / "e.db"), approve_irreversible=True
    )
    first = guard.run("charge", target="c", payload={"a": 1, "b": 2}, execute=lambda: "x")
    second = guard.run("charge", target="c", payload={"b": 2, "a": 1}, execute=lambda: "y")
    guard.close()
    assert first.effect_id == second.effect_id


# ── defect 2: a failure is never recorded as a commit ───────────────────────


def test_failed_execution_is_recorded_as_failed(tmp_path):
    """Upstream set ``status = Committed`` and only then checked
    ``if !result.success``, so a throwing executor left a committed record and
    the next attempt was served a receipt for work that never happened."""
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))

    def boom():
        raise RuntimeError("vendor returned 500")

    with pytest.raises(RuntimeError):
        guard.run("edit_file", target="a.py", payload={"patch": "x"}, execute=boom)

    record = guard.effects()[0]
    assert record.status == FAILED
    assert record.status != COMMITTED
    assert record.response_hash is None
    guard.close()


def test_a_failed_idempotent_effect_can_be_retried(tmp_path):
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))
    attempts: list[int] = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return "ok"

    with pytest.raises(RuntimeError):
        guard.run("write_file", target="a.py", payload={"body": "x"}, execute=flaky)

    outcome = guard.run("write_file", target="a.py", payload={"body": "x"}, execute=flaky)
    guard.close()
    assert outcome.executed is True
    assert outcome.status == COMMITTED
    assert len(attempts) == 2


def test_a_failed_irreversible_effect_is_not_retried_automatically(tmp_path):
    """A failed charge may have partially applied. Re-issuing it is a second
    real-world event, so the guard refuses and makes the caller decide."""
    guard = SideEffectGuard(
        session_id="s1", store_path=str(tmp_path / "e.db"), approve_irreversible=True
    )

    def boom():
        raise RuntimeError("timeout")

    with pytest.raises(RuntimeError):
        guard.run("charge", target="cus_1", payload={"cents": 100}, execute=boom)

    with pytest.raises(EffectNotRunnable, match="must be resolved by hand"):
        guard.run("charge", target="cus_1", payload={"cents": 100}, execute=lambda: "ok")
    guard.close()


def test_a_failed_effect_recovers_to_retry_or_query(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    with pytest.raises(RuntimeError):
        guard.run(
            "edit_file",
            target="a.py",
            payload={},
            execute=lambda: (_ for _ in ()).throw(RuntimeError("x")),
        )
    record = guard.effects()[0]
    decision = guard.decide(record)
    guard.close()
    assert decision.action == "retry"
    assert decision.strategy is RecoveryStrategy.COMPENSATE


# ── defect 3: a crash mid-call is visible, not invisible ────────────────────


def test_crash_between_claim_and_commit_is_detectable(tmp_path):
    """Upstream had no state between PENDING and COMMITTED. A process that died
    after the external call but before recording it left a record that looked
    like "never started", so the agent retried blind — for an irreversible
    effect, a second real-world event.

    The crash is simulated by claiming the effect and then abandoning the guard
    without running it, which is what a ``kill -9`` between the two leaves on
    disk.
    """
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))
    record, _ = guard._claim(
        action_type="charge",
        target="cus_1",
        payload={"cents": 100},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    guard.close()

    recovered = SideEffectGuard(session_id="s1", store_path=str(db))
    pending = recovered.store.unresolved("s1")
    assert len(pending) == 1
    assert pending[0].status == EXECUTING

    decisions = recovered.recover()
    recovered.close()
    assert decisions[0].action == "query_external"
    assert decisions[0].strategy is RecoveryStrategy.QUERY_AND_CONFIRM
    assert "second real-world event" in decisions[0].detail


def test_adopting_an_executing_effect_refuses_to_run(tmp_path):
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))
    record, _ = guard._claim(
        action_type="charge",
        target="cus_1",
        payload={},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
    )
    guard.store.set_status(record.effect_id, EXECUTING)

    with pytest.raises(EffectNotRunnable, match="may have reached the outside world"):
        guard.run(
            "charge",
            target="cus_1",
            payload={},
            execute=lambda: "should not run",
        )
    guard.close()


def test_a_crashed_idempotent_effect_recovers_to_replay(tmp_path):
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))
    record, _ = guard._claim(
        action_type="write_file",
        target="a.py",
        payload={},
        effect_class=EffectClass.IDEMPOTENT,
        preconditions=(),
        context={},
        approve_irreversible=False,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    decision = guard.decide(guard.store.get(record.effect_id))
    guard.close()
    assert decision.action == "replay"
    assert decision.strategy is RecoveryStrategy.REPLAY


def test_a_crashed_reversible_effect_without_compensation_replays(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    record, _ = guard._claim(
        action_type="edit_file",
        target="a.py",
        payload={},
        effect_class=EffectClass.REVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=False,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    decision = guard.decide(guard.store.get(record.effect_id))
    guard.close()
    assert decision.action == "replay"


def test_a_crashed_reversible_effect_with_compensation_compensates(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    record, _ = guard._claim(
        action_type="edit_file",
        target="a.py",
        payload={},
        effect_class=EffectClass.REVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=False,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    guard.store.set_compensation(record.effect_id, {"original_sha256": "abc"})
    decision = guard.decide(guard.store.get(record.effect_id))
    guard.close()
    assert decision.action == "compensate_and_replay"
    assert decision.strategy is RecoveryStrategy.COMPENSATE


def test_committed_effect_recovers_to_cached(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    outcome = guard.run("write_file", target="a.py", payload={}, execute=lambda: "done")
    decision = guard.decide(outcome.record)
    guard.close()
    assert decision.action == "use_cached"
    assert "do not call again" in decision.detail


def test_operator_can_close_out_an_unresolved_effect(tmp_path):
    """The human end of recovery: an operator checks the vendor dashboard and
    records what they found."""
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    record, _ = guard._claim(
        action_type="charge",
        target="cus_1",
        payload={},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    resolved = guard.resolve(record.effect_id, COMMITTED, response_hash="confirmed_by_ops")
    guard.close()
    assert resolved.status == COMMITTED
    assert resolved.response_hash == "confirmed_by_ops"


# ── preconditions ───────────────────────────────────────────────────────────


def test_irreversible_effects_require_explicit_approval(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    with pytest.raises(PreconditionFailed, match="irreversible effects require explicit approval"):
        guard.run("charge", target="cus_1", payload={}, execute=lambda: "charged")
    guard.close()


def test_approval_allows_the_irreversible_effect(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    outcome = guard.run(
        "charge", target="cus_1", payload={}, execute=lambda: "charged", approve_irreversible=True
    )
    guard.close()
    assert outcome.executed is True


def test_approval_defaults_can_be_set_per_guard(tmp_path):
    guard = SideEffectGuard(
        session_id="s1", store_path=str(tmp_path / "e.db"), approve_irreversible=True
    )
    outcome = guard.run("charge", target="cus_1", payload={}, execute=lambda: "ok")
    guard.close()
    assert outcome.executed is True


def test_preconditions_are_per_guard_not_only_the_builtin_gate():
    """Upstream's check was ``precond == "deny"`` — one magic string. Any other
    precondition value was silently ignored, which is documented protection that
    does not exist."""
    seen: list[str] = []

    @precondition("budget", "budget exhausted for this session")
    def within_budget(context):
        seen.append("checked")
        return context.get("remaining_cents", 0) >= 100

    guard = SideEffectGuard(session_id="s1")
    with pytest.raises(PreconditionFailed, match="budget exhausted for this session"):
        guard.run(
            "write_file",
            target="a.py",
            payload={},
            execute=lambda: "wrote",
            preconditions=(within_budget,),
            context={"remaining_cents": 50},
        )

    assert seen == ["checked"], "the check must actually run"
    assert guard.effects() == [], "a refused effect must not consume the key"

    outcome = guard.run(
        "write_file",
        target="a.py",
        payload={},
        execute=lambda: "wrote",
        preconditions=(within_budget,),
        context={"remaining_cents": 500},
    )
    guard.close()
    assert outcome.executed is True


def test_a_refusal_names_the_precondition():
    @precondition("clean_tree", "working tree has uncommitted changes")
    def clean_tree(context):
        return not context.get("dirty")

    guard = SideEffectGuard(session_id="s1")
    with pytest.raises(PreconditionFailed) as info:
        guard.run(
            "deploy",
            target="prod",
            payload={},
            execute=lambda: "deployed",
            preconditions=(clean_tree,),
            context={"dirty": True},
            approve_irreversible=True,
        )
    guard.close()
    assert info.value.precondition == "clean_tree"
    assert "uncommitted changes" in str(info.value)


def test_a_raising_precondition_refuses_rather_than_passing():
    """A check that cannot complete has not established that the effect is safe."""

    @precondition("needs_a_context_key")
    def broken(context):
        return context["definitely_missing"] > 0

    guard = SideEffectGuard(session_id="s1")
    with pytest.raises(PreconditionFailed, match="precondition raised"):
        guard.run(
            "write_file",
            target="a.py",
            payload={},
            execute=lambda: "wrote",
            preconditions=(broken,),
        )
    guard.close()


def test_preconditions_run_before_the_effect_does():
    ran: list[str] = []

    def execute():
        ran.append("executed")
        return "ok"

    @precondition("never", "always refuses")
    def never(context):
        return False

    guard = SideEffectGuard(session_id="s1")
    with pytest.raises(PreconditionFailed):
        guard.run("write_file", target="a.py", preconditions=(never,), execute=execute)
    guard.close()
    assert ran == [], "the outside world must not be touched before the check passes"


def test_the_old_magic_string_would_no_longer_be_special():
    """Pin the port: ``"deny"`` is not a magic value any more. A precondition is
    a callable, so a caller passing the literal string gets a clear error rather
    than silent acceptance."""
    guard = SideEffectGuard(session_id="s1")
    with pytest.raises((AttributeError, TypeError)):
        guard.run(
            "write_file",
            target="a.py",
            execute=lambda: "wrote",
            preconditions=("deny",),  # type: ignore[arg-type]
        )
    guard.close()


# ── concurrency ─────────────────────────────────────────────────────────────


def test_concurrent_attempts_run_the_effect_once(tmp_path):
    """Two threads racing on the same logical effect: exactly one executes."""
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s1", store_path=str(db))
    calls: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def charge():
        with lock:
            calls.append(1)
        return "charged"

    outcomes: list[object] = []
    errors: list[Exception] = []

    def attempt():
        barrier.wait()
        try:
            outcomes.append(
                guard.run(
                    "charge",
                    target="cus_1",
                    payload={"cents": 100},
                    execute=charge,
                    approve_irreversible=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    guard.close()

    executed = [o for o in outcomes if getattr(o, "executed", False)]
    assert len(calls) == 1, f"the effect ran {len(calls)} times"
    assert len(executed) <= 1
    # Losers either adopt the winner's record or are told it is in flight.
    assert len(outcomes) + len(errors) == 4


# ── guard construction and introspection ────────────────────────────────────


def test_empty_session_id_is_refused():
    with pytest.raises(ValueError, match="session_id must not be empty"):
        SideEffectGuard(session_id="")


def test_effects_are_scoped_to_their_session(tmp_path):
    db = tmp_path / "effects.db"
    one = SideEffectGuard(session_id="s1", store_path=str(db))
    two = SideEffectGuard(session_id="s2", store_path=str(db))
    one.run("write_file", target="a.py", payload={}, execute=lambda: "1")
    two.run("write_file", target="a.py", payload={}, execute=lambda: "2")

    assert len(one.effects()) == 1
    assert len(two.effects()) == 1
    assert one.effects()[0].effect_id != two.effects()[0].effect_id
    one.close()
    two.close()


def test_summary_counts_by_status(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    guard.run("write_file", target="a.py", payload={}, execute=lambda: "1")
    guard.run("write_file", target="b.py", payload={}, execute=lambda: "2")
    summary = guard.summary()
    guard.close()
    assert summary == {"committed": 2}


def test_outcome_serialises_for_a_caller(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    outcome = guard.run("write_file", target="a.py", payload={}, execute=lambda: "ok")
    payload = outcome.to_dict()
    guard.close()
    assert payload["effect_class"] == "idempotent"
    assert payload["status"] == "committed"
    assert payload["executed"] is True
    assert payload["effect_id"] == outcome.effect_id


def test_recovery_decision_serialises_for_a_caller(tmp_path):
    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    record, _ = guard._claim(
        action_type="deploy",
        target="prod",
        payload={},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
    )
    guard.store.set_status(record.effect_id, EXECUTING)
    decision = guard.decide(guard.store.get(record.effect_id))
    payload = decision.to_dict()
    guard.close()
    assert payload["strategy"] == "query_and_confirm"
    assert payload["action"] == "query_external"


def test_guard_errors_share_a_base_class():
    assert issubclass(PreconditionFailed, GuardError)
    assert issubclass(EffectNotRunnable, GuardError)


# ── end to end ──────────────────────────────────────────────────────────────


def test_the_billing_double_charge_scenario(tmp_path):
    """The whole reason this package exists, in one test.

    A session charges a customer, the process dies, and the agent retries. The
    customer is charged once.
    """
    db = tmp_path / "effects.db"
    ledger: list[tuple[str, int]] = []

    def charge(customer: str, cents: int) -> dict[str, str]:
        ledger.append((customer, cents))
        return {"charge_id": f"ch_{len(ledger)}", "amount_cents": str(cents)}

    session_one = SideEffectGuard(session_id="run-42", store_path=str(db))
    first = session_one.run(
        "charge",
        target="cus_123",
        payload={"customer": "cus_123", "cents": 4200},
        execute=lambda: charge("cus_123", 4200),
        approve_irreversible=True,
    )
    session_one.close()  # process exits

    session_two = SideEffectGuard(session_id="run-42", store_path=str(db))
    second = session_two.run(
        "charge",
        target="cus_123",
        payload={"customer": "cus_123", "cents": 4200},
        execute=lambda: charge("cus_123", 4200),
        approve_irreversible=True,
    )
    summary = session_two.summary()
    session_two.close()

    assert ledger == [("cus_123", 4200)], "the customer must be charged exactly once"
    assert first.executed is True
    assert second.executed is False
    assert second.effect_id == first.effect_id
    assert summary == {"committed": 1}


def test_a_refused_effect_leaves_no_trace_in_the_ledger(tmp_path):
    ledger: list[str] = []

    @precondition("dry_run", "deployment window is closed")
    def window_open(context):
        return bool(context.get("window"))

    guard = SideEffectGuard(session_id="s1", store_path=str(tmp_path / "e.db"))
    with pytest.raises(PreconditionFailed):
        guard.run(
            "deploy",
            target="prod",
            payload={},
            execute=lambda: ledger.append("deployed"),
            preconditions=(window_open,),
            context={"window": False},
            approve_irreversible=True,
        )
    assert ledger == []
    assert guard.summary() == {}, "a refused effect must not be recorded as an attempt"
    guard.close()


def test_sqlite_unique_constraint_exists_at_the_schema_level(tmp_path):
    """Cheaper than the guard: prove the constraint is declared, so a future
    refactor that drops it fails loudly rather than silently losing idempotency."""
    db = tmp_path / "effects.db"
    EffectStore(db).close()
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='side_effects'"
    ).fetchone()
    indexes = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='side_effects'"
    ).fetchall()
    conn.close()
    assert row is not None
    schema = row[0] + " " + " ".join(i[0] or "" for i in indexes)
    assert "UNIQUE (session_id, idempotency_key)" in schema


# ── scope: the durability boundary ──────────────────────────────────────────


def test_scope_dedupes_across_a_new_session(tmp_path):
    """A restart *is* a new session id, so a session-scoped key cannot recognise
    the retry. This is the defect the scope parameter exists for."""
    db = tmp_path / "effects.db"
    calls: list[int] = []
    first = SideEffectGuard(session_id="session-1", store_path=str(db), approve_irreversible=True)
    first.run(
        "charge",
        target="cus_1",
        payload={"cents": 100},
        execute=lambda: calls.append(1) or "ch_1",
        scope=durable_scope("order-1", "charge", "cus_1"),
    )
    first.close()

    second = SideEffectGuard(session_id="session-2", store_path=str(db), approve_irreversible=True)
    outcome = second.run(
        "charge",
        target="cus_1",
        payload={"cents": 100},
        execute=lambda: calls.append(1) or "ch_1",
        scope=durable_scope("order-1", "charge", "cus_1"),
    )
    second.close()

    assert outcome.executed is False
    assert len(calls) == 1, "the customer must be charged once across the restart"


def test_without_a_scope_a_new_session_charges_again(tmp_path):
    """The behaviour scope replaces, pinned so the difference stays visible."""
    db = tmp_path / "effects.db"
    calls: list[int] = []
    for session in ("session-1", "session-2"):
        guard = SideEffectGuard(session_id=session, store_path=str(db), approve_irreversible=True)
        guard.run(
            "charge",
            target="cus_1",
            payload={"cents": 100},
            execute=lambda: calls.append(1) or "ch_1",
        )
        guard.close()
    assert len(calls) == 2, "session-scoped keys cannot see across a restart"


def test_different_scopes_are_different_effects(tmp_path):
    db = tmp_path / "effects.db"
    calls: list[int] = []
    for order in ("order-1", "order-2"):
        guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
        guard.run(
            "charge",
            target="cus_1",
            payload={"cents": 100},
            execute=lambda: calls.append(1) or "ch",
            scope=durable_scope(order, "charge", "cus_1"),
        )
        guard.close()
    assert len(calls) == 2, "two orders are two charges"


def test_a_scope_does_not_collide_with_an_unscoped_key(tmp_path):
    db = tmp_path / "effects.db"
    calls: list[int] = []
    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    guard.run("charge", target="c", payload={}, execute=lambda: calls.append(1) or "a")
    guard.run(
        "charge",
        target="c",
        payload={},
        execute=lambda: calls.append(1) or "b",
        scope=durable_scope("R", "charge", "c"),
    )
    guard.close()
    assert len(calls) == 2, "the scoped namespace must not alias the session namespace"


def test_durable_scope_rejects_an_empty_run_id():
    with pytest.raises(ValueError, match="run_id must not be empty"):
        durable_scope("")


def test_durable_scope_is_stable_and_discriminating():
    assert durable_scope("r", "a") == durable_scope("r", "a")
    assert durable_scope("r", "a") != durable_scope("r", "b")
    assert durable_scope("r1", "a") != durable_scope("r2", "a")


# ── the async entry point ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_arun_runs_the_coroutine_once(tmp_path):
    db = tmp_path / "effects.db"
    calls: list[int] = []

    async def work() -> str:
        calls.append(1)
        return "done"

    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    scope = durable_scope("R", "charge", "c")
    first = await guard.arun("charge", target="c", payload={}, execute=work, scope=scope)
    second = await guard.arun("charge", target="c", payload={}, execute=work, scope=scope)
    guard.close()

    assert first.executed is True
    assert second.executed is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_arun_records_an_unknown_outcome_on_failure(tmp_path):
    db = tmp_path / "effects.db"

    async def boom() -> str:
        raise RuntimeError("vendor 500")

    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    with pytest.raises(RuntimeError):
        await guard.arun("charge", target="c", payload={}, execute=boom)
    records = guard.effects()
    guard.close()
    assert records[0].status == FAILED


@pytest.mark.asyncio
async def test_arun_marks_executing_before_awaiting(tmp_path):
    """The property that makes recovery sound: the claim is durable before the
    outside world is touched, so a death mid-call leaves evidence."""
    db = tmp_path / "effects.db"
    observed: list[str] = []

    async def work() -> str:
        observed.append(guard.effects()[0].status)
        return "done"

    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    await guard.arun("charge", target="c", payload={}, execute=work)
    guard.close()
    assert observed == [EXECUTING], "the record must already be `executing` when the tool runs"


# ── recovered retry ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovered_retry_runs_after_an_operator_confirms_no_charge(tmp_path):
    """The step between recover() and getting on with the work."""
    db = tmp_path / "effects.db"
    calls: list[int] = []

    async def work() -> str:
        calls.append(1)
        return "charged"

    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)

    async def boom() -> str:
        raise RuntimeError("timeout")

    with pytest.raises(RuntimeError):
        await guard.arun("charge", target="cus_1", payload={}, execute=boom)
    record = guard.effects()[0]
    assert record.status == FAILED

    # An operator checked the vendor: nothing went through.
    guard.resolve(record.effect_id, COMPENSATED)
    outcome = await guard.recovered_retry(record.effect_id, execute=work)
    guard.close()

    assert outcome.executed is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_recovered_retry_refuses_an_unresolved_effect(tmp_path):
    """The whole point: an unknown outcome must not be retried blind."""
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    record, _ = guard._claim(
        action_type="charge",
        target="cus_1",
        payload={},
        effect_class=EffectClass.IRREVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=True,
    )
    guard.store.set_status(record.effect_id, EXECUTING)

    with pytest.raises(EffectNotRunnable, match="outcome is still unknown"):
        await guard.recovered_retry(record.effect_id, execute=lambda: "should not run")
    guard.close()


@pytest.mark.asyncio
async def test_recovered_retry_serves_a_committed_effect(tmp_path):
    db = tmp_path / "effects.db"
    calls: list[int] = []

    async def work() -> str:
        calls.append(1)
        return "charged"

    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    first = await guard.arun("charge", target="c", payload={}, execute=work)
    again = await guard.recovered_retry(first.effect_id, execute=work)
    guard.close()

    assert first.executed is True
    assert again.executed is False, "a committed effect is never re-run"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_recovered_retry_refuses_a_pending_effect(tmp_path):
    """A pending effect is a fresh attempt, which belongs to run()."""
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s", store_path=str(db), approve_irreversible=True)
    record, _ = guard._claim(
        action_type="edit_file",
        target="a.py",
        payload={},
        effect_class=EffectClass.REVERSIBLE,
        preconditions=(),
        context={},
        approve_irreversible=False,
    )
    with pytest.raises(EffectNotRunnable, match="use run"):
        await guard.recovered_retry(record.effect_id, execute=lambda: "x")
    guard.close()


@pytest.mark.asyncio
async def test_recovered_retry_refuses_an_unknown_effect_id(tmp_path):
    db = tmp_path / "effects.db"
    guard = SideEffectGuard(session_id="s", store_path=str(db))
    with pytest.raises(Exception, match="no such effect"):
        await guard.recovered_retry("se_nope", execute=lambda: "x")
    guard.close()


# ── taxonomy: the conservative defaults ─────────────────────────────────────


def test_unknown_tools_are_irreversible_not_read_only():
    """A guard that guessed 'read' for an unknown name would wave through the
    one call it cannot vouch for."""
    assert classify("search_docs", "docs") is EffectClass.IRREVERSIBLE
    assert classify("mystery_tool", "/tmp/x") is EffectClass.IRREVERSIBLE
