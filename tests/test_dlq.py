import asyncio
import random

from service.metrics import Metrics
from service.models import Outcome
from service.replay import replay_dead_letters
from service.resilience.breaker import BreakerState, CircuitBreaker
from service.resilience.executor import CallExecutor
from service.resilience.retry import RetryPolicy
from service.store.call_log import CallLog
from service.store.db import Database
from service.store.dlq import SqliteDLQ
from tests.conftest import OK, FakeClock, FakePipeline, make_event, r503


async def test_enqueue_is_idempotent_and_updates_last_failed_at(db: Database) -> None:
    dlq = SqliteDLQ(db)
    payload = make_event().model_dump(mode="json")
    await dlq.enqueue("evt-1", payload, "retries_exhausted", 3, "http_503")
    first = await dlq.get("evt-1")
    await asyncio.sleep(0.01)
    await dlq.enqueue("evt-1", payload, "circuit_open", 0, None)
    second = await dlq.get("evt-1")

    assert first is not None and second is not None
    assert (await dlq.stats())["total"] == 1
    assert second.first_failed_at == first.first_failed_at
    assert second.last_failed_at > first.last_failed_at
    assert second.reason == "circuit_open"
    assert second.payload == payload


async def test_list_filters_by_status_and_reason_prefix(db: Database) -> None:
    dlq = SqliteDLQ(db)
    payload = make_event().model_dump(mode="json")
    await dlq.enqueue("a", payload, "circuit_open", 0, None)
    await dlq.enqueue("b", payload, "non_retryable:http_400:bad", 1, "http_400")
    await dlq.enqueue("c", payload, "retries_exhausted", 3, "http_503")
    await dlq.mark_replayed("c")

    assert [e.event_id for e in await dlq.list(reason="non_retryable")] == ["b"]
    assert {e.event_id for e in await dlq.list(status="pending")} == {"a", "b"}
    assert [e.event_id for e in await dlq.list(status="replayed")] == ["c"]
    assert len(await dlq.list(limit=2)) == 2


def build_executor(db: Database, pipeline: FakePipeline, clock: FakeClock) -> CallExecutor:
    return CallExecutor(
        pipeline=pipeline,
        breaker=CircuitBreaker(clock=clock),
        call_log=CallLog(db),
        dlq=SqliteDLQ(db),
        metrics=Metrics(),
        policy=RetryPolicy(),
        outcome_seed=7,
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )


async def test_replay_success_marks_replayed_and_updates_call_log(
    db: Database, clock: FakeClock
) -> None:
    pipeline = FakePipeline([r503(), r503(), r503()])  # first call fails, replay succeeds
    executor = build_executor(db, pipeline, clock)
    first = await executor.handle_call(make_event("evt-1"))
    assert first.outcome is Outcome.FAILED_DOWNSTREAM

    dlq = SqliteDLQ(db)
    summary = await replay_dead_letters(
        executor, dlq, Metrics(), event_ids=["evt-1"], sleep=clock.sleep
    )
    entry = await dlq.get("evt-1")
    stored = await CallLog(db).get("evt-1")

    assert summary.replayed == 1 and summary.failed == 0
    assert entry is not None and entry.status == "replayed" and entry.replay_count == 1
    assert stored is not None and stored.outcome is not Outcome.FAILED_DOWNSTREAM
    assert stored.status == "succeeded"


async def test_replay_failure_bumps_replay_count(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline(default=r503())
    executor = build_executor(db, pipeline, clock)
    await executor.handle_call(make_event("evt-1"))
    dlq = SqliteDLQ(db)

    for expected in (1, 2):
        clock.advance(60)  # let the breaker cool off between attempts
        summary = await replay_dead_letters(
            executor, dlq, Metrics(), replay_all=True, sleep=clock.sleep
        )
        entry = await dlq.get("evt-1")
        assert summary.failed == 1
        assert entry is not None
        assert entry.status == "replay_failed"
        assert entry.replay_count == expected


async def test_replay_is_rate_limited_and_reports_missing(db: Database, clock: FakeClock) -> None:
    pipeline = FakePipeline([r503()] * 9, default=OK)
    executor = build_executor(db, pipeline, clock)
    for i in range(3):
        await executor.handle_call(make_event(f"evt-{i}"))
    clock.sleeps.clear()

    summary = await replay_dead_letters(
        executor,
        SqliteDLQ(db),
        Metrics(),
        event_ids=["evt-0", "evt-1", "evt-2", "ghost"],
        rate_per_s=4.0,
        sleep=clock.sleep,
    )
    assert summary.replayed == 3
    assert summary.missing == ["ghost"]
    assert clock.sleeps.count(0.25) == 3  # one 1/rate gap between each of the 4 ids


async def test_replay_does_not_wait_for_one_call_before_starting_the_next(
    db: Database, clock: FakeClock
) -> None:
    failing = build_executor(db, FakePipeline(default=r503()), clock)
    for i in range(5):
        await failing.handle_call(make_event(f"evt-{i}"))

    release = asyncio.Event()

    class GatedPipeline(FakePipeline):
        """Every call blocks until all five are in flight; serial replay would deadlock."""

        async def process(self, event_id: str, audio_ref: str):
            self.calls.append(event_id)
            if len(self.calls) == 5:
                release.set()
            await release.wait()
            return OK

    gated = GatedPipeline()
    summary = await asyncio.wait_for(
        replay_dead_letters(
            build_executor(db, gated, clock),
            SqliteDLQ(db),
            Metrics(),
            replay_all=True,
            sleep=clock.sleep,
        ),
        timeout=2.0,
    )
    assert summary.replayed == 5
    assert sorted(gated.calls) == [f"evt-{i}" for i in range(5)]


def tripped_executor(db: Database, pipeline: FakePipeline, clock: FakeClock) -> CallExecutor:
    """An executor whose breaker has just opened (10s cool-off, a single probe slot)."""
    breaker = CircuitBreaker(clock=clock, open_s=10, half_open_max=1)
    for _ in range(100):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    return CallExecutor(
        pipeline=pipeline,
        breaker=breaker,
        call_log=CallLog(db),
        dlq=SqliteDLQ(db),
        metrics=Metrics(),
        policy=RetryPolicy(),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )


async def dead_letter(db: Database, clock: FakeClock, n: int) -> None:
    failing = build_executor(db, FakePipeline(default=r503()), clock)
    for i in range(n):
        await failing.handle_call(make_event(f"evt-{i}"))


async def test_replay_waits_out_an_open_breaker_and_probes_before_fanning_out(
    db: Database, clock: FakeClock
) -> None:
    """Regression: replaying right after an outage met an open (or half-open) breaker, and
    most of the backlog failed instantly as circuit_open instead of being replayed."""
    await dead_letter(db, clock, 5)

    class SlowPipeline(FakePipeline):
        async def process(self, event_id: str, audio_ref: str):
            self.calls.append(event_id)
            await asyncio.sleep(0.01)  # a success is slower than a fast-failing caller
            return OK

    executor = tripped_executor(db, SlowPipeline(), clock)
    summary = await replay_dead_letters(
        executor, SqliteDLQ(db), Metrics(), replay_all=True, rate_per_s=1000, sleep=clock.sleep
    )
    assert summary.replayed == 5 and summary.failed == 0 and summary.aborted == 0
    assert executor.breaker_state is BreakerState.CLOSED


async def test_replay_stops_when_the_probe_shows_the_dependency_still_down(
    db: Database, clock: FakeClock
) -> None:
    await dead_letter(db, clock, 5)
    executor = tripped_executor(db, FakePipeline(default=r503()), clock)
    dlq = SqliteDLQ(db)

    summary = await replay_dead_letters(
        executor, dlq, Metrics(), replay_all=True, rate_per_s=1000, sleep=clock.sleep
    )
    stats = await dlq.stats()
    assert (summary.failed, summary.aborted) == (1, 4)
    assert (stats["replay_failed"], stats["pending"]) == (1, 4)  # untouched, not burned
