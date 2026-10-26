"""Dead-letter queue: failed call events, kept inspectable and replayable.

The executor and replay code depend only on the DLQBackend protocol, so Round 2 can add an
SQS-backed implementation without touching them. Each entry stores the full original
payload, which is what makes replay possible: the event can be resubmitted exactly as it
first arrived.

Entries are keyed by event_id: a call that fails again (e.g. a failed replay) updates its
existing entry (last_failed_at, attempts, reason) instead of adding a duplicate.

Status lifecycle: pending -> replayed (replay succeeded) or replay_failed (still failing;
can be replayed again later). replay_count counts every replay attempt.
"""

# Deferred annotations: the interface has a method named `list`, which would otherwise
# shadow the built-in in later type hints inside the class body.
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from service.store.db import Database

DLQStatus = Literal["pending", "replayed", "replay_failed"]


@dataclass(frozen=True)
class DeadLetter:
    event_id: str
    payload: dict[str, Any]
    reason: str
    attempts: int
    last_error: str | None
    first_failed_at: str
    last_failed_at: str
    status: DLQStatus
    replay_count: int


class DLQBackend(Protocol):
    async def enqueue(
        self,
        event_id: str,
        payload: dict[str, Any],
        reason: str,
        attempts: int,
        last_error: str | None,
    ) -> None: ...

    async def list(
        self, status: DLQStatus | None = None, reason: str | None = None, limit: int = 100
    ) -> list[DeadLetter]: ...

    async def get(self, event_id: str) -> DeadLetter | None: ...

    async def mark_replayed(self, event_id: str) -> None: ...

    async def mark_replay_failed(self, event_id: str) -> None: ...

    async def stats(self) -> dict[str, int]: ...

    async def existing_ids(self, event_ids: list[str]) -> set[str]: ...


ENQUEUE = """
INSERT INTO dead_letters (event_id, payload_json, reason, attempts, last_error,
                          first_failed_at, last_failed_at, status)
VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
ON CONFLICT (event_id) DO UPDATE SET
    payload_json = excluded.payload_json,
    reason = excluded.reason,
    attempts = excluded.attempts,
    last_error = excluded.last_error,
    last_failed_at = excluded.last_failed_at,
    status = 'pending'
"""


class SqliteDLQ:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def enqueue(
        self,
        event_id: str,
        payload: dict[str, Any],
        reason: str,
        attempts: int,
        last_error: str | None,
    ) -> None:
        now = _now()
        params = (event_id, json.dumps(payload), reason, attempts, last_error, now, now)

        def write(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(ENQUEUE, params)

        await self._db.run(write)

    async def list(
        self, status: DLQStatus | None = None, reason: str | None = None, limit: int = 100
    ) -> list[DeadLetter]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if reason:
            # Prefix match, so reason=non_retryable finds "non_retryable:http_400:...".
            clauses.append("reason LIKE ? || '%'")
            params.append(reason)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM dead_letters {where} ORDER BY first_failed_at, event_id LIMIT ?"

        def read(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(sql, [*params, limit]).fetchall()

        return [_row_to_dead_letter(r) for r in await self._db.run(read)]

    async def get(self, event_id: str) -> DeadLetter | None:
        def read(conn: sqlite3.Connection) -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM dead_letters WHERE event_id = ?", (event_id,)
            ).fetchone()

        row = await self._db.run(read)
        return _row_to_dead_letter(row) if row else None

    async def mark_replayed(self, event_id: str) -> None:
        await self._set_status(event_id, "replayed")

    async def mark_replay_failed(self, event_id: str) -> None:
        await self._set_status(event_id, "replay_failed")

    async def _set_status(self, event_id: str, status: DLQStatus) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "UPDATE dead_letters SET status = ?, replay_count = replay_count + 1 "
                    "WHERE event_id = ?",
                    (status, event_id),
                )

        await self._db.run(write)

    async def stats(self) -> dict[str, int]:
        def read(conn: sqlite3.Connection) -> dict[str, int]:
            rows = conn.execute("SELECT status, COUNT(*) FROM dead_letters GROUP BY status")
            counts = {status: n for status, n in rows}
            return {
                "pending": counts.get("pending", 0),
                "replayed": counts.get("replayed", 0),
                "replay_failed": counts.get("replay_failed", 0),
                "total": sum(counts.values()),
            }

        return await self._db.run(read)

    async def existing_ids(self, event_ids: list[str]) -> set[str]:
        def read(conn: sqlite3.Connection) -> set[str]:
            found: set[str] = set()
            for i in range(0, len(event_ids), 500):
                chunk = event_ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT event_id FROM dead_letters WHERE event_id IN ({marks})", chunk
                )
                found.update(r[0] for r in rows)
            return found

        return await self._db.run(read)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dead_letter(row: sqlite3.Row) -> DeadLetter:
    return DeadLetter(
        event_id=row["event_id"],
        payload=json.loads(row["payload_json"]),
        reason=row["reason"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        first_failed_at=row["first_failed_at"],
        last_failed_at=row["last_failed_at"],
        status=row["status"],
        replay_count=row["replay_count"],
    )
