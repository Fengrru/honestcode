"""Adversarial corpus: agent edits that can silently land on the wrong value.

Design rules
------------

Each scenario is a fake episode: an agent has read a file, picked an edit, and
sent a search string that is **not** a verbatim copy of the text it is aiming
at. A reformat, a re-chunking, or a stale quote means the ``exact`` strategy
cannot resolve it, and the cascade falls through to formatting-tolerant
strategies. That fall-through is where a value can be lost.

Every scenario therefore has:

* ``target`` — the region the agent means to edit, carrying the values that
  appear in ``search``.
* ``distractor`` — a near-miss twin that shares the shape and the surrounding
  identifiers but states a different value. It is placed **earlier in the file**
  so a formatting-tolerant strategy reaches it first.

Two failure modes, not equally bad:

* **no match** — nothing changed and the caller is told. Recoverable: the agent
  re-reads the file and retries.
* **silent drift** — the edit landed on ``distractor`` and the call reported
  success. A value the caller never named is gone, and nothing at the call site
  distinguishes that from a correct edit.

``intended_region`` is declared explicitly and ``verify_corpus`` asserts it is
consistent with the text, so a scenario cannot quietly disagree with what it
claims to test. ``expected_after`` / ``forbidden_after`` describe the two
possible file states for reporting.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Scenario", "SCENARIOS"]


@dataclass(frozen=True)
class Scenario:
    id: str
    what: str
    file: str
    search: str
    replace: str
    target: str
    distractor: str
    intended_region: str
    expected_after: str
    forbidden_after: str

    @property
    def intended_is_target(self) -> bool:
        return self.intended_region == self.target

    @property
    def competing_region(self) -> str:
        """The region the caller did *not* name."""
        return self.distractor if self.intended_is_target else self.target


SCENARIOS: list[Scenario] = [
    Scenario(
        id="port_reindented",
        what=(
            "agent quotes a constructor call without its indentation; the sibling "
            "call, which it must not touch, states a different port"
        ),
        file="""\
from dataclasses import dataclass


@dataclass
class Server:
    port: int = 8000


def secondary() -> Server:
    return Server(
        port=8880,
    )


def primary() -> Server:
    return Server(
        port=8080,
    )
""",
        search="""return Server(
        port=8080,
    )""",
        replace="""return Server(
        port=9090,
    )""",
        target="""    return Server(
        port=8080,
    )""",
        distractor="""    return Server(
        port=8880,
    )""",
        intended_region="""    return Server(
        port=8080,
    )""",
        expected_after="port=9090,",
        forbidden_after="port=8880,",
    ),
    Scenario(
        id="timeout_ms_vs_seconds",
        what=(
            "agent quotes a one-line policy block without its indentation; the "
            "sibling policy states the same quantity in a unit ten times smaller"
        ),
        file="""\
RETRY_POLICY = (
    "timeout_ms",
    3000,
)

SLOW_PATH = (
    "timeout_ms",
    30000,
)


def apply(policy):
    return dict(zip(policy[::2], policy[1::2]))
""",
        search="""    "timeout_ms",
    30000,
)""",
        replace="""    "timeout_ms",
    60000,
)""",
        target="""SLOW_PATH = (
    "timeout_ms",
    30000,
)""",
        distractor="""RETRY_POLICY = (
    "timeout_ms",
    3000,
)""",
        intended_region="""SLOW_PATH = (
    "timeout_ms",
    30000,
)""",
        expected_after="60000,",
        forbidden_after="3000,",
    ),
    Scenario(
        id="page_size_rechunked",
        what=(
            "agent re-chunks a multi-line call into one line before searching; a "
            "sibling call to the same endpoint differs only in page_size"
        ),
        file="""\
def page_large(client):
    return client.get(
        "/v1/items",
        page_size=1000,
        timeout=30,
    )


def page_small(client):
    return client.get(
        "/v1/items",
        page_size=100,
        timeout=30,
    )
""",
        search="""return client.get("/v1/items", page_size=100, timeout=30)""",
        replace="""return client.get("/v1/items", page_size=250, timeout=30)""",
        target="""    return client.get(
        "/v1/items",
        page_size=100,
        timeout=30,
    )""",
        distractor="""    return client.get(
        "/v1/items",
        page_size=1000,
        timeout=30,
    )""",
        intended_region="""    return client.get(
        "/v1/items",
        page_size=100,
        timeout=30,
    )""",
        expected_after="page_size=250",
        forbidden_after="page_size=1000",
    ),
    Scenario(
        id="retries_rechunked",
        what=(
            "agent collapses a request call to one line; the neighbouring helper "
            "uses the same shape with a ten-times smaller retry budget"
        ),
        file="""\
def first(client):
    return client.request(
        "GET",
        "/users",
        retries=3,
        backoff=1.5,
    )


def second(client):
    return client.request(
        "GET",
        "/users",
        retries=30,
        backoff=1.5,
    )
""",
        search="""return client.request("GET", "/users", retries=30, backoff=1.5)""",
        replace="""return client.request("GET", "/users", retries=300, backoff=1.5)""",
        target="""    return client.request(
        "GET",
        "/users",
        retries=30,
        backoff=1.5,
    )""",
        distractor="""    return client.request(
        "GET",
        "/users",
        retries=3,
        backoff=1.5,
    )""",
        intended_region="""    return client.request(
        "GET",
        "/users",
        retries=30,
        backoff=1.5,
    )""",
        expected_after="retries=300",
        forbidden_after="retries=3,",
    ),
    Scenario(
        id="amount_cents_inline_vs_block",
        what=(
            "agent quotes a rule table in block form; the sibling table, written "
            "inline, carries the same keys with a ten-times larger cap"
        ),
        file="""\
REFUND_RULES = {"auto_approve_cents": 1000, "review_cents": 50000}

CHARGEBACK_RULES = {
    "auto_approve_cents": 100,
    "review_cents": 50000,
}


def load_chargebacks():
    return dict(CHARGEBACK_RULES, source="chargebacks")
""",
        search="""CHARGEBACK_RULES = {
    "auto_approve_cents": 100,
    "review_cents": 50000,
}""",
        replace="""CHARGEBACK_RULES = {
    "auto_approve_cents": 250,
    "review_cents": 50000,
}""",
        target="""CHARGEBACK_RULES = {
    "auto_approve_cents": 100,
    "review_cents": 50000,
}""",
        distractor="""REFUND_RULES = {"auto_approve_cents": 1000, "review_cents": 50000}""",
        intended_region="""CHARGEBACK_RULES = {
    "auto_approve_cents": 100,
    "review_cents": 50000,
}""",
        expected_after='auto_approve_cents": 250',
        forbidden_after="1000",
    ),
    Scenario(
        id="ratio_inline_vs_block",
        what=(
            "agent quotes a similarity gate inline; the sibling gate, written as a "
            "block, states a different rate"
        ),
        file="""\
STRICT_GATE = {
    "similarity": 0.90,
    "min_samples": 50,
}

LOOSE_GATE = {"similarity": 0.95, "min_samples": 50}


def gate_for(name):
    return {"strict": STRICT_GATE, "loose": LOOSE_GATE}[name]
""",
        search="""LOOSE_GATE = {"similarity": 0.95, "min_samples": 50}""",
        replace="""LOOSE_GATE = {"similarity": 0.75, "min_samples": 50}""",
        target="""LOOSE_GATE = {"similarity": 0.95, "min_samples": 50}""",
        distractor="""STRICT_GATE = {
    "similarity": 0.90,
    "min_samples": 50,
}""",
        intended_region="""LOOSE_GATE = {"similarity": 0.95, "min_samples": 50}""",
        expected_after="0.75",
        forbidden_after="0.90",
    ),
    Scenario(
        id="sql_template_wrapped",
        what=(
            "agent quotes a query as one line; the sibling query selects the same "
            "columns from a similarly named table"
        ),
        file="""\
def load_active(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM users_active WHERE deleted_at IS NULL")
    return cur.fetchall()


def load_archived(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name FROM users_archived WHERE deleted_at IS NOT NULL"
    )
    return cur.fetchall()
""",
        search="""cur.execute("SELECT id, name FROM users_archived WHERE deleted_at IS NOT NULL")""",
        replace="""cur.execute("SELECT id, name FROM accounts_archived WHERE deleted_at IS NOT NULL")""",
        target="""    cur.execute(
        "SELECT id, name FROM users_archived WHERE deleted_at IS NOT NULL"
    )""",
        distractor="""    cur.execute("SELECT id, name FROM users_active WHERE deleted_at IS NULL")""",
        intended_region="""    cur.execute(
        "SELECT id, name FROM users_archived WHERE deleted_at IS NOT NULL"
    )""",
        expected_after="accounts_archived",
        forbidden_after="users_active",
    ),
    Scenario(
        id="float_tolerance_trailing",
        what=(
            "agent drops a statement's indentation; the sibling assertion's "
            "tolerance shares every digit but the last"
        ),
        file="""\
def test_median_is_close():
    assert math.isclose(compute_median(data), 4.25, rel_tol=0.010)


def test_mean_is_close():
    assert math.isclose(compute_mean(data), 4.25, rel_tol=0.01)
""",
        search="""assert math.isclose(compute_mean(data), 4.25, rel_tol=0.01)""",
        replace="""assert math.isclose(compute_mean(data), 4.25, rel_tol=0.005)""",
        target="""    assert math.isclose(compute_mean(data), 4.25, rel_tol=0.01)""",
        distractor="""    assert math.isclose(compute_median(data), 4.25, rel_tol=0.010)""",
        intended_region="""    assert math.isclose(compute_mean(data), 4.25, rel_tol=0.01)""",
        expected_after="0.005",
        forbidden_after="0.010",
    ),
    Scenario(
        id="env_port_nested",
        what=(
            "agent quotes an environment's nested block without its key, where a "
            "sibling environment's block differs only in the port it states"
        ),
        file="""\
ENVIRONMENTS = {
    "staging": {
        "host": "stack.internal",
        "port": 8080,
    },
    "production": {
        "host": "stack.internal",
        "port": 8880,
    },
}
""",
        search="""    "production": {
        "host": "stack.internal",
        "port": 8880,
    }""",
        replace="""    "production": {
        "host": "stack.internal",
        "port": 9090,
    }""",
        target="""    "production": {
        "host": "stack.internal",
        "port": 8880,
    }""",
        distractor="""    "staging": {
        "host": "stack.internal",
        "port": 8080,
    }""",
        intended_region="""    "production": {
        "host": "stack.internal",
        "port": 8880,
    }""",
        expected_after='"port": 9090,',
        forbidden_after='"port": 8080,',
    ),
    Scenario(
        id="limit_trailing_zero",
        what=(
            "agent quotes a query chain without its indentation; the archival "
            "query differs only by a trailing zero in its limit"
        ),
        file="""\
def archive(session):
    return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(500)
        .all()
    )


def recent(session):
    return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )
""",
        search="""return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )""",
        replace="""return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(2000)
        .all()
    )""",
        target="""    return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )""",
        distractor="""    return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(500)
        .all()
    )""",
        intended_region="""    return (
        session.query(Event)
        .order_by(Event.created_at.desc())
        .limit(50)
        .all()
    )""",
        expected_after=".limit(2000)",
        forbidden_after=".limit(500)",
    ),
]
