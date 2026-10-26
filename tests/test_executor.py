import asyncio
import random

import httpx
import pytest

from service.config import Settings
from service.main import create_app
from service.metrics import Metrics
from service.models import Outcome
from service.resilience.breaker import CircuitBreaker
from service.resilience.executor import CallExecutor, PersistenceError, sample_outcome
from service.resilience.retry import RetryPolicy
from service.store.call_log import SqliteCallLog
from service.store.db import Database
from service.store.dlq import SqliteDLQ
from tests.conftest import OK, FakeClock, FakePipeline, make_event, r400, r503


def build(db: Database, pipeline: FakePipeline, clock: FakeClock, **kw: object) -> CallExecutor:
    return CallExecutor(
        pipeline=pipeline,
        breaker=kw.pop("breaker", None) or CircuitBreaker(clock=clock),  # type: ignore[arg-type]
        call_log=SqliteCallLog(db),
        dlq=SqliteDLQ(db),
        metrics=kw.pop("metrics", None) or Metrics(),  # type: ignore[arg-type]
        policy=RetryPolicy(),
        outcome_seed=7,
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
        **kw,  # type: ignore[arg-type]
    )


async def test_success_assigns_business_outcome(db: Database, clock: FakeClock) -> None:
    result = await build(db, FakePipeline([r503(), OK]), clock).handle_call(make_event())
    assert result.status == "succeeded"
    assert result.outcome is not Outcome.FAILED_DOWNSTREAM
    assert result.retry_count == 1
    assert (await SqliteCallLog(db).get("evt-1")) == result
    assert (await SqliteDLQ(db).stats())["total"] == 0


async def test_duplicate_event_id_calls_pipeline_once(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline()
    executor = build(db, pipeline, clock)
    first = await executor.handle_call(make_event())
    second = await executor.handle_call(make_event())
    assert pipeline.calls == ["evt-1"]
    assert first == second


async def test_concurrent_duplicates_share_one_pipeline_call(
    db: Database, clock: FakeClock
) -> None:
    pipeline = FakePipeline()
    executor = build(db, pipeline, clock)
    results = await asyncio.gather(*(executor.handle_call(make_event()) for _ in range(5)))
    assert pipeline.calls == ["evt-1"]
    assert len({r.model_dump_json() for r in results}) == 1


async def test_retries_exhausted_goes_to_call_log_and_dlq(db: Database, clock: FakeClock) -> None:
    metrics = Metrics()
    result = await build(db, FakePipeline(default=r503()), clock, metrics=metrics).handle_call(
        make_event()
    )
    entry = await SqliteDLQ(db).get("evt-1")
    assert result.outcome is Outcome.FAILED_DOWNSTREAM
    assert result.dlq_reason == "retries_exhausted"
    assert result.retry_count == 2
    assert entry is not None and entry.attempts == 3 and entry.status == "pending"
    assert entry.payload["event_id"] == "evt-1"
    assert metrics.dlq_enqueued_total.values[(("reason", "retries_exhausted"),)] == 1
    assert metrics.attempts_total.total() == 3
    assert metrics.downstream_errors_total.total() == 3


async def test_non_retryable_goes_to_dlq_without_retry(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline([r400()])
    result = await build(db, pipeline, clock).handle_call(make_event())
    assert len(pipeline.calls) == 1
    assert result.dlq_reason is not None
    assert result.dlq_reason.startswith("non_retryable:")


async def test_breaker_open_records_circuit_open_in_dlq(db: Database, clock: FakeClock) -> None:
    breaker = CircuitBreaker(clock=clock)
    for _ in range(100):
        breaker.record_failure()
    pipeline = FakePipeline()
    result = await build(db, pipeline, clock, breaker=breaker).handle_call(make_event())
    entry = await SqliteDLQ(db).get("evt-1")
    assert pipeline.calls == []
    assert result.dlq_reason == "circuit_open"
    assert entry is not None and entry.reason == "circuit_open" and entry.attempts == 0


async def test_unexpected_pipeline_bug_is_still_recorded(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline([ZeroDivisionError("bug")])
    result = await build(db, pipeline, clock).handle_call(make_event())
    assert result.dlq_reason == "internal_error:ZeroDivisionError"
    assert await SqliteCallLog(db).get("evt-1") is not None


async def test_turns_per_call_runs_each_turn_with_retry(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline([OK, r503(), OK, OK])
    result = await build(db, pipeline, clock, turns_per_call=3).handle_call(make_event())
    assert result.status == "succeeded"
    assert len(pipeline.calls) == 4
    assert result.retry_count == 1


async def test_persistence_failure_raises(db: Database, clock: FakeClock) -> None:
    executor = build(db, FakePipeline(), clock)

    async def broken(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    executor._call_log.upsert = broken  # type: ignore[method-assign]
    with pytest.raises(PersistenceError):
        await executor.handle_call(make_event())


def test_outcome_sampling_mix_and_determinism() -> None:
    outcomes = [sample_outcome(f"e{i}", seed=1)[0] for i in range(20_000)]
    share = {o: outcomes.count(o) / len(outcomes) for o in set(outcomes)}
    assert abs(share[Outcome.CONNECTED] - 0.60) < 0.02
    assert abs(share[Outcome.NO_ANSWER] - 0.20) < 0.02
    assert abs(share[Outcome.PTP_TAKEN] - 0.15) < 0.02
    assert abs(share[Outcome.DISPUTE] - 0.05) < 0.01
    assert sample_outcome("x", 1) == sample_outcome("x", 1)
    for i in range(200):
        outcome, duration = sample_outcome(f"d{i}", 3)
        assert duration == 0 if outcome is Outcome.NO_ANSWER else 30 <= duration <= 120


# ---------- API-level tests ----------


async def api(tmp_path, pipeline: FakePipeline):
    settings = Settings(db_path=str(tmp_path / "api.db"), outcome_seed=1, backoff_base_s=0.0)
    app = create_app(settings, pipeline=pipeline, setup_logging=False)
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc")


EVENT = {
    "event_id": "a1f2c3d4-0001",  # Predixion's sample ID: not a valid UUID, must be accepted
    "campaign_id": "CAMP-2026-09-A",
    "borrower_bucket": "0-30 DPD",
    "product_type": "bnpl",
    "timestamp": "2026-09-01T09:03:12Z",
    "call_duration_s": 47,
    "outcome": "connected",  # input outcome is accepted but ignored
    "retry_count": 0,
}


async def test_api_accepts_sample_data_and_ignores_input_outcome(tmp_path) -> None:
    app, client = await api(tmp_path, FakePipeline(default=r503()))
    async with app.router.lifespan_context(app), client:
        response = await client.post("/calls", json=EVENT)
        assert response.status_code == 200
        assert response.json()["outcome"] == "failed_downstream"
        assert (await client.get("/calls/a1f2c3d4-0001")).status_code == 200


async def test_api_rejects_invalid_input_with_422(tmp_path) -> None:
    app, client = await api(tmp_path, FakePipeline())
    async with app.router.lifespan_context(app), client:
        bad = {**EVENT, "borrower_bucket": "120 DPD"}
        assert (await client.post("/calls", json=bad)).status_code == 422
        missing = {k: v for k, v in EVENT.items() if k != "campaign_id"}
        assert (await client.post("/calls", json=missing)).status_code == 422


async def test_api_returns_500_when_persistence_fails(tmp_path) -> None:
    app, client = await api(tmp_path, FakePipeline())
    async with app.router.lifespan_context(app), client:

        async def broken(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        app.state.executor._call_log.upsert = broken
        response = await client.post("/calls", json=EVENT)
        assert response.status_code == 500
        assert response.json()["detail"] == "persistence_failed"
        assert "persistence_errors_total 1" in (await client.get("/metrics")).text


async def test_api_dlq_replay_and_stats(tmp_path) -> None:
    pipeline = FakePipeline([r503(), r503(), r503()])
    app, client = await api(tmp_path, pipeline)
    async with app.router.lifespan_context(app), client:
        await client.post("/calls", json=EVENT)
        listing = (await client.get("/dlq", params={"status": "pending"})).json()
        assert listing["count"] == 1
        assert (await client.get("/dlq/a1f2c3d4-0001")).json()["reason"] == "retries_exhausted"
        assert (await client.get("/dlq/ghost")).status_code == 404

        assert (await client.post("/dlq/replay", json={})).status_code == 422
        replay = await client.post("/dlq/replay", json={"all": True, "rate_per_s": 100})
        assert replay.json()["replayed"] == 1

        stats = (await client.get("/stats")).json()
        assert stats["received"] == 1
        assert stats["failed_downstream"] == 0
        assert stats["dlq_pending"] == 0
        assert stats["dlq_replayed"] == 1

        metrics = (await client.get("/metrics")).text
        assert "steadfast_breaker_state 0" in metrics
        assert 'steadfast_replays_total{result="replayed"} 1' in metrics

        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).json() == {
            "call_log_writable": True,
            "pipeline_client": True,
        }
        lookup = await client.post("/calls/lookup", json={"event_ids": ["a1f2c3d4-0001", "zz"]})
        assert lookup.json() == {
            "requested": 2,
            "in_call_log": 1,
            "in_dlq": 1,
            "missing": ["zz"],
        }
