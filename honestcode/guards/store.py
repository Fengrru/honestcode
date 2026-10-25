"""Durable store for side-effect records.

The upstream implementation kept its records in a ``Vec``. An idempotency key
that vanishes when the process exits is not an idempotency key: the crash that
loses the record is the same crash after which the agent retries the effect.
This store is the fix, and everything else in this package is deliberately thin
around it.

The uniqueness constraint is the whole point and is enforced by the database,
not by a read-then-write check in Python:

    UNIQUE (session_id, idempotency_key)

A single connection guarded by a lock is used rather than a pool, because the
guard is called on the agent's tool-call path where a lock is cheaper than
contention and because SQLite serialises writers regardless.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from honestcode.guards.taxonomy import EffectClass

__all__ = ["EffectRecord", "EffectStatus", "EffectStore", "StoreError"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS side_effects (
    effect_id       TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    target          TEXT NOT NULL DEFAULT '',
    effect_class    TEXT NOT NULL,
    payload_hash    TEXT NOT NULL DEFAULT '',
    preconditions   TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    scope           TEXT,
    response_hash   TEXT,
    compensation    TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    committed_at    INTEGER,
    UNIQUE (session_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_side_effects_status
    ON side_effects (status);

CREATE INDEX IF NOT EXISTS idx_side_effects_session
    ON side_effects (session_id, created_at);
"""


class StoreError(RuntimeError):
    """The store could not be read or written."""


class EffectStatus(str, Enum):
    """Lifecycle states, as a ``str`` enum so they survive JSON round-trips.

    The set is deliberately larger than upstream's ``PENDING/COMMITTED/
    COMPENSATED/FAILED``. ``EXECUTING`` is the state that makes recovery sound:
    an effect is marked before the outside world is touched, so a process that
    dies mid-call leaves evidence that the outcome is *unknown* rather than
    evidence that nothing happened. Without it, a crash between "call succeeded"
    and "record committed" is indistinguishable from "never called", and the
    agent retries an external mutation.
    """

    PENDING = "pending"
    """Intent recorded; the effect has not been attempted yet."""

    EXECUTING = "executing"
    """The effect may have reached the outside world. Outcome unknown until the
    record advances — this is the state a crash leaves behind."""

    COMMITTED = "committed"
    """The effect ran and its result is recorded. Never re-run; serve the receipt."""

    FAILED = "failed"
    """The effect raised. Whether it partially applied is unknown; the class
    decides whether retrying is safe."""

    COMPENSATED = "compensated"
    """A reversible effect was undone. Safe to attempt again."""

    @property
    def bare(self) -> str:
        return self.value


# Historical module-level names; guard.py, __init__.py and tests import these,
# so the enum members are re-exported under the same identities.
PENDING = EffectStatus.PENDING
EXECUTING = EffectStatus.EXECUTING
COMMITTED = EffectStatus.COMMITTED
FAILED = EffectStatus.FAILED
COMPENSATED = EffectStatus.COMPENSATED

_ALL_STATUSES = frozenset(EffectStatus)

#: Statuses where re-running the effect would be a second real-world event.
ACTIVE_STATUSES = frozenset({PENDING, EXECUTING, COMMITTED})


@dataclass(frozen=True)
class EffectRecord:
    """One side effect the agent intended, and how far it got."""

    effect_id: str
    session_id: str
    idempotency_key: str
    action_type: str
    target: str
    effect_class: EffectClass
    payload_hash: str
    preconditions: tuple[str, ...]
    status: EffectStatus
    #: The durable scope the caller supplied, if any. Scoped records live in a
    #: namespace derived from this value; keeping it on the row is what lets
    #: recovery query the records for a run id after a restart.
    scope: str | None = None
    response_hash: str | None = None
    compensation: dict[str, Any] | None = None
    created_at: int = 0
    updated_at: int = 0
    committed_at: int | None = None

    @property
    def is_active(self) -> bool:
        """True when re-issuing this intent must not run the effect again."""
        return self.status in ACTIVE_STATUSES

    @property
    def outcome_known(self) -> bool:
        """True when the record states definitively whether the effect ran."""
        return self.status in (COMMITTED, COMPENSATED)

    def to_dict(self) -> dict[str, Any]:
        """The receipt shape handed back to a caller."""
        out: dict[str, Any] = {
            "effect_id": self.effect_id,
            "session_id": self.session_id,
            "idempotency_key": self.idempotency_key,
            "action_type": self.action_type,
            "target": self.target,
            "effect_class": self.effect_class.bare,
            "status": self.status.bare,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.scope is not None:
            out["scope"] = self.scope
        if self.response_hash is not None:
            out["response_hash"] = self.response_hash
        if self.committed_at is not None:
            out["committed_at"] = self.committed_at
        if self.compensation is not None:
            out["compensation"] = self.compensation
        return out


def _now_millis() -> int:
    return int(time.time() * 1000)


def _row_to_record(row: sqlite3.Row) -> EffectRecord:
    return EffectRecord(
        effect_id=row["effect_id"],
        session_id=row["session_id"],
        idempotency_key=row["idempotency_key"],
        action_type=row["action_type"],
        target=row["target"],
        effect_class=EffectClass(row["effect_class"]),
        payload_hash=row["payload_hash"],
        preconditions=tuple(json.loads(row["preconditions"])),
        status=EffectStatus(row["status"]),
        scope=row["scope"],
        response_hash=row["response_hash"],
        compensation=json.loads(row["compensation"]) if row["compensation"] else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        committed_at=int(row["committed_at"]) if row["committed_at"] is not None else None,
    )


class EffectStore:
    """SQLite-backed record store. One instance per agent session database.

    ``path`` may be ``":memory:"`` for tests. A file path is created along with
    its parent directory, because the usual failure mode for a durable store is
    that nobody made the directory.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                self.path,
                check_same_thread=False,
                isolation_level=None,  # explicit transactions
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            # Databases created before scope was persisted get the column added
            # in place; the index is created after the migration because an old
            # table has no scope column for it to reference.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(side_effects)")}
            if "scope" not in columns:
                self._conn.execute("ALTER TABLE side_effects ADD COLUMN scope TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_side_effects_scope ON side_effects (scope)"
            )
        except sqlite3.Error as exc:  # pragma: no cover - environment dependent
            raise StoreError(f"cannot open effect store at {self.path}: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> EffectStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── writes ──────────────────────────────────────────────────────────────

    def insert(
        self,
        *,
        effect_id: str,
        session_id: str,
        idempotency_key: str,
        action_type: str,
        target: str,
        effect_class: EffectClass,
        payload_hash: str,
        preconditions: tuple[str, ...],
        status: EffectStatus = PENDING,
        scope: str | None = None,
    ) -> EffectRecord | None:
        """Insert a new record, or return ``None`` if the key already exists.

        The uniqueness check is the database's, so two concurrent attempts at
        the same effect cannot both insert — the second gets ``None`` and must
        adopt the existing record instead. Doing this with a SELECT first would
        leave a window between the read and the write.
        """
        now = _now_millis()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO side_effects (
                        effect_id, session_id, idempotency_key, action_type, target,
                        effect_class, payload_hash, preconditions, status, scope,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        effect_id,
                        session_id,
                        idempotency_key,
                        action_type,
                        target,
                        effect_class.value,
                        payload_hash,
                        json.dumps(list(preconditions)),
                        status.value,
                        scope,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            return self.get(effect_id)

    def set_status(
        self,
        effect_id: str,
        status: EffectStatus,
        *,
        response_hash: str | None = None,
    ) -> EffectRecord:
        """Advance a record's lifecycle state."""
        if status not in _ALL_STATUSES:
            raise StoreError(f"unknown status: {status!r}")
        now = _now_millis()
        committed_at = now if status == COMMITTED else None
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE side_effects
                   SET status = ?,
                       updated_at = ?,
                       response_hash = COALESCE(?, response_hash),
                       committed_at = COALESCE(?, committed_at)
                 WHERE effect_id = ?
                """,
                (status.value, now, response_hash, committed_at, effect_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"no such effect: {effect_id}")
        record = self.get(effect_id)
        if record is None:  # pragma: no cover - rowcount check above covers this
            raise StoreError(f"no such effect: {effect_id}")
        return record

    def transition_if_status(
        self,
        effect_id: str,
        allowed: tuple[EffectStatus, ...],
        status: EffectStatus,
    ) -> bool:
        """Atomically advance a record only while it is in one of *allowed*.

        The read-then-write form this replaces let two concurrent retries of a
        FAILED effect both observe ``failed`` and both go on to execute the
        effect. Returns ``True`` only for the caller that actually performed the
        transition; everyone else gets ``False`` and must adopt or refuse.
        """
        if status not in _ALL_STATUSES:
            raise StoreError(f"unknown status: {status!r}")
        allowed_values = tuple(s.value for s in allowed)
        if not allowed_values:
            return False
        placeholders = ", ".join("?" for _ in allowed_values)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE side_effects SET status = ?, updated_at = ? "
                f"WHERE effect_id = ? AND status IN ({placeholders})",
                (status.value, _now_millis(), effect_id, *allowed_values),
            )
            return cur.rowcount == 1

    def set_compensation(self, effect_id: str, data: dict[str, Any]) -> EffectRecord:
        """Attach the information needed to undo a reversible effect."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE side_effects SET compensation = ?, updated_at = ? WHERE effect_id = ?",
                (json.dumps(data), _now_millis(), effect_id),
            )
            if cur.rowcount == 0:
                raise StoreError(f"no such effect: {effect_id}")
        record = self.get(effect_id)
        if record is None:  # pragma: no cover
            raise StoreError(f"no such effect: {effect_id}")
        return record

    # ── reads ───────────────────────────────────────────────────────────────

    def get(self, effect_id: str) -> EffectRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM side_effects WHERE effect_id = ?", (effect_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def find_by_key(self, session_id: str, idempotency_key: str) -> EffectRecord | None:
        """The lookup that makes an effect idempotent across restarts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM side_effects WHERE session_id = ? AND idempotency_key = ?",
                (session_id, idempotency_key),
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_records(
        self,
        *,
        session_id: str | None = None,
        status: EffectStatus | None = None,
        scope: str | None = None,
        limit: int = 1000,
    ) -> list[EffectRecord]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if scope is not None:
            # Scoped records live under a namespace session id, so filtering by
            # the caller's session missed them entirely; scope is the column
            # that survives a restart and identifies the run they belong to.
            clauses.append("scope = ?")
            params.append(scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM side_effects {where} ORDER BY created_at, effect_id LIMIT ?",  # noqa: S608 - clauses are literals
                params,
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def unresolved(
        self, session_id: str | None = None, *, scope: str | None = None
    ) -> list[EffectRecord]:
        """Records whose outcome is *unknown* — the crash-recovery queue.

        These are the effects an agent must resolve before doing anything else,
        because for each of them the outside world may or may not have already
        been changed. Pass *scope* to ask the cross-restart question for one
        durable run id; the scoped record's session id is a derived namespace
        and cannot be supplied by the caller.
        """
        clauses = ["status IN (?, ?)"]
        params: list[Any] = [EXECUTING.value, PENDING.value]
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM side_effects WHERE {' AND '.join(clauses)} "  # noqa: S608
                "ORDER BY created_at, effect_id",
                params,
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def count_by_status(
        self, session_id: str | None = None, *, scope: str | None = None
    ) -> dict[str, int]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) AS n FROM side_effects {where} GROUP BY status",  # noqa: S608
                params,
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}
