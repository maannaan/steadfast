"""The call log: one row per call event, the record that every call was handled.

Rows are upserted by event_id, so processing the same event twice (a client retrying a
POST, or a DLQ replay that turns a failure into a success) updates one row instead of
creating duplicates.

The executor and API depend only on CallLogBackend. Locally a single process owns a SQLite
file; on AWS several tasks sit behind a load balancer, so they share a DynamoDB table
(store/dynamo_call_log.py) and any task can answer "was this call recorded?".
"""

import sqlite3
from datetime import datetime
from typing import Protocol

from service.models import CallResult, Outcome
from service.store.db import Database


class CallLogBackend(Protocol):
    async def upsert(
        self, result: CallResult, received_at: datetime, finished_at: datetime
    ) -> None: ...

    async def get(self, event_id: str) -> CallResult | None: ...

    async def existing_ids(self, event_ids: list[str]) -> set[str]: ...

    async def stats(self) -> dict[str, int]: ...

    async def healthy(self) -> bool:
        """Can we read and write the log right now? Backs the /ready probe."""
        ...


UPSERT = """
INSERT INTO call_log (event_id, campaign_id, borrower_bucket, product_type, event_timestamp,
                      received_at, finished_at, outcome, retry_count, call_duration_s,
                      latency_ms, status, dlq_reason)
VALUES (:event_id, :campaign_id, :borrower_bucket, :product_type, :event_timestamp,
        :received_at, :finished_at, :outcome, :retry_count, :call_duration_s,
        :latency_ms, :status, :dlq_reason)
ON CONFLICT (event_id) DO UPDATE SET
    finished_at = excluded.finished_at,
    outcome = excluded.outcome,
    retry_count = excluded.retry_count,
    call_duration_s = excluded.call_duration_s,
    latency_ms = excluded.latency_ms,
    status = excluded.status,
    dlq_reason = excluded.dlq_reason
"""


class SqliteCallLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def healthy(self) -> bool:
        return await self._db.is_writable()

    async def upsert(
        self, result: CallResult, received_at: datetime, finished_at: datetime
    ) -> None:
        params = {
            "event_id": result.event_id,
            "campaign_id": result.campaign_id,
            "borrower_bucket": result.borrower_bucket.value,
            "product_type": result.product_type.value if result.product_type else None,
            "event_timestamp": result.timestamp.isoformat(),
            "received_at": received_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "outcome": result.outcome.value,
            "retry_count": result.retry_count,
            "call_duration_s": result.call_duration_s,
            "latency_ms": result.latency_ms,
            "status": result.status,
            "dlq_reason": result.dlq_reason,
        }

        def write(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(UPSERT, params)

        await self._db.run(write)

    async def get(self, event_id: str) -> CallResult | None:
        def read(conn: sqlite3.Connection) -> sqlite3.Row | None:
            return conn.execute("SELECT * FROM call_log WHERE event_id = ?", (event_id,)).fetchone()

        row = await self._db.run(read)
        return _row_to_result(row) if row else None

    async def existing_ids(self, event_ids: list[str]) -> set[str]:
        """Which of these event_ids have a call_log row (used to prove zero unaccounted)."""

        def read(conn: sqlite3.Connection) -> set[str]:
            found: set[str] = set()
            for i in range(0, len(event_ids), 500):  # stay under SQLite's variable limit
                chunk = event_ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT event_id FROM call_log WHERE event_id IN ({marks})", chunk
                )
                found.update(r[0] for r in rows)
            return found

        return await self._db.run(read)

    async def stats(self) -> dict[str, int]:
        def read(conn: sqlite3.Connection) -> dict[str, int]:
            row = conn.execute(
                """
                SELECT
                  COUNT(*) AS received,
                  COALESCE(SUM(status = 'succeeded' AND retry_count = 0), 0) AS first_try,
                  COALESCE(SUM(status = 'succeeded' AND retry_count > 0), 0) AS after_retry,
                  COALESCE(SUM(outcome = ?), 0) AS failed
                FROM call_log
                """,
                (Outcome.FAILED_DOWNSTREAM.value,),
            ).fetchone()
            return {
                "received": row["received"],
                "succeeded_first_try": row["first_try"],
                "succeeded_after_retry": row["after_retry"],
                "failed_downstream": row["failed"],
            }

        return await self._db.run(read)


def _row_to_result(row: sqlite3.Row) -> CallResult:
    return CallResult(
        event_id=row["event_id"],
        campaign_id=row["campaign_id"],
        borrower_bucket=row["borrower_bucket"],
        product_type=row["product_type"],
        timestamp=row["event_timestamp"],
        outcome=row["outcome"],
        call_duration_s=row["call_duration_s"],
        retry_count=row["retry_count"],
        latency_ms=row["latency_ms"],
        status=row["status"],
        dlq_reason=row["dlq_reason"],
    )
