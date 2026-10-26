from datetime import UTC, datetime

from service.models import CallResult, Outcome
from service.store.call_log import SqliteCallLog
from service.store.db import Database


def result(event_id: str = "evt-1", **overrides: object) -> CallResult:
    data: dict[str, object] = {
        "event_id": event_id,
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": "0-30 DPD",
        "product_type": "bnpl",
        "timestamp": "2026-09-01T09:03:12Z",
        "outcome": "failed_downstream",
        "call_duration_s": 0,
        "retry_count": 2,
        "latency_ms": 900,
        "status": "failed",
        "dlq_reason": "retries_exhausted",
        **overrides,
    }
    return CallResult.model_validate(data)


NOW = datetime(2026, 9, 1, tzinfo=UTC)


async def test_wal_mode_and_schema(db: Database) -> None:
    mode = await db.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
    tables = await db.run(
        lambda c: {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    )
    assert mode == "wal"
    assert {"call_log", "dead_letters"} <= tables
    assert await db.is_writable()


async def test_upsert_is_idempotent_and_updates_in_place(db: Database) -> None:
    log = SqliteCallLog(db)
    await log.upsert(result(), NOW, NOW)
    await log.upsert(
        result(outcome="connected", status="succeeded", dlq_reason=None, call_duration_s=40),
        NOW,
        NOW,
    )
    count = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM call_log").fetchone()[0])
    stored = await log.get("evt-1")
    assert count == 1
    assert stored is not None
    assert stored.outcome is Outcome.CONNECTED
    assert stored.dlq_reason is None


async def test_stats_and_existing_ids(db: Database) -> None:
    log = SqliteCallLog(db)
    await log.upsert(result("a", outcome="connected", status="succeeded", retry_count=0), NOW, NOW)
    await log.upsert(result("b", outcome="dispute", status="succeeded", retry_count=1), NOW, NOW)
    await log.upsert(result("c"), NOW, NOW)
    assert await log.stats() == {
        "received": 3,
        "succeeded_first_try": 1,
        "succeeded_after_retry": 1,
        "failed_downstream": 1,
    }
    ids = [f"x{i}" for i in range(1200)] + ["a", "c"]  # crosses the 500-id chunk size
    assert await log.existing_ids(ids) == {"a", "c"}


async def test_get_missing_returns_none(db: Database) -> None:
    assert await SqliteCallLog(db).get("nope") is None
