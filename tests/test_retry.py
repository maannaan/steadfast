import random

import pytest

from service.pipeline.base import NonRetryableError, RetryableError
from service.resilience.breaker import BreakerState, CircuitBreaker
from service.resilience.retry import RetryGiveUpError, RetryPolicy, backoff_delay, call_with_retry
from tests.conftest import OK, FakeClock, FakePipeline, r400, r503

POLICY = RetryPolicy()


def make_breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(clock=clock)


async def run(pipeline: FakePipeline, clock: FakeClock, policy: RetryPolicy = POLICY, **kw):
    return await call_with_retry(
        lambda: pipeline.process("e1", "synthetic"),
        policy=policy,
        breaker=kw.pop("breaker", None) or make_breaker(clock),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(1),
        **kw,
    )


async def test_503_then_success_uses_one_retry(clock: FakeClock) -> None:
    pipeline = FakePipeline([r503(), OK])
    result, retries = await run(pipeline, clock)
    assert result == OK
    assert retries == 1
    assert len(pipeline.calls) == 2
    assert len(clock.sleeps) == 1


async def test_first_try_success_uses_no_retry(clock: FakeClock) -> None:
    pipeline = FakePipeline([OK])
    _, retries = await run(pipeline, clock)
    assert retries == 0
    assert clock.sleeps == []


async def test_non_retryable_is_never_retried(clock: FakeClock) -> None:
    pipeline = FakePipeline([r400(), OK])
    with pytest.raises(NonRetryableError) as info:
        await run(pipeline, clock)
    assert len(pipeline.calls) == 1
    assert info.value.attempts == 1
    assert clock.sleeps == []


async def test_three_503s_give_up_as_retryable(clock: FakeClock) -> None:
    pipeline = FakePipeline([r503(), r503(), r503(), OK])
    with pytest.raises(RetryableError) as info:
        await run(pipeline, clock)
    assert isinstance(info.value, RetryGiveUpError)
    assert info.value.reason == "retries_exhausted"
    assert info.value.attempts == 3
    assert len(pipeline.calls) == 3


def test_full_jitter_stays_within_bounds() -> None:
    rng = random.Random(0)
    base, cap = 0.2, 2.0
    for n in range(8):
        upper = min(cap, base * 2**n)
        samples = [backoff_delay(n, base, cap, rng) for _ in range(2000)]
        assert all(0 <= s <= upper for s in samples)
        # Full jitter uses the whole interval, not just its top end.
        assert min(samples) < upper * 0.1
        assert max(samples) > upper * 0.9


async def test_deadline_stops_retries(clock: FakeClock) -> None:
    def slow_failure() -> RetryableError:
        clock.advance(3.0)  # each attempt burns 3s of the 8s deadline
        return r503()

    pipeline = FakePipeline(default=slow_failure)  # type: ignore[arg-type]
    policy = RetryPolicy(max_attempts=10, base_s=1.0, cap_s=4.0, deadline_s=8.0)
    with pytest.raises(RetryGiveUpError) as info:
        await run(pipeline, clock, policy)
    assert info.value.reason == "deadline_exceeded"
    # 3s per attempt against an 8s deadline leaves room for 2-3 attempts, never 10.
    assert 2 <= len(pipeline.calls) <= 3


async def test_attempt_timeout_counts_as_retryable(clock: FakeClock) -> None:
    import asyncio

    class Hanging:
        calls = 0

        async def process(self, event_id: str, audio_ref: str):
            Hanging.calls += 1
            if Hanging.calls == 1:
                await asyncio.sleep(1)  # longer than the attempt timeout below
            return OK

    hanging = Hanging()
    result, retries = await call_with_retry(
        lambda: hanging.process("e1", "synthetic"),
        policy=RetryPolicy(attempt_timeout_s=0.05),
        breaker=make_breaker(clock),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(1),
    )
    assert result == OK
    assert retries == 1


async def test_stops_retrying_when_breaker_opens_mid_call(clock: FakeClock) -> None:
    breaker = CircuitBreaker(window=4, min_calls=2, failure_threshold=0.5, clock=clock)
    pipeline = FakePipeline(default=r503())
    with pytest.raises(RetryGiveUpError) as info:
        await run(pipeline, clock, RetryPolicy(max_attempts=5), breaker=breaker)
    assert info.value.reason == "circuit_open"
    assert breaker.state is BreakerState.OPEN
    assert len(pipeline.calls) == 2  # opened after the 2nd failure; no 3rd attempt


async def test_on_attempt_hook_sees_every_attempt(clock: FakeClock) -> None:
    seen: list[Exception | None] = []
    pipeline = FakePipeline([r503(), OK])
    await run(pipeline, clock, on_attempt=seen.append)
    assert len(seen) == 2
    assert isinstance(seen[0], RetryableError)
    assert seen[1] is None
