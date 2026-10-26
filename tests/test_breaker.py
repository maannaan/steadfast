import contextlib
import random

from service.resilience.breaker import BreakerState, CircuitBreaker
from service.resilience.retry import RetryGiveUpError, RetryPolicy, call_with_retry
from tests.conftest import FakeClock, FakePipeline, r503


def transitions_recorder() -> tuple[list[tuple[str, str]], object]:
    seen: list[tuple[str, str]] = []
    return seen, lambda old, new: seen.append((old.value, new.value))


def test_opens_at_half_failures_once_min_calls_reached(clock: FakeClock) -> None:
    breaker = CircuitBreaker(window=20, min_calls=10, failure_threshold=0.5, clock=clock)
    for _ in range(4):
        breaker.record_success()
    for _ in range(5):
        breaker.record_failure()
    # 9 attempts, 5/9 failed: above threshold but below min_calls, so still closed.
    assert breaker.state is BreakerState.CLOSED
    breaker.record_success()  # 10 attempts, 5/10 = 50%... checked on failures only
    breaker.record_failure()  # 11 attempts, 6/11 >= 50% and >= min_calls
    assert breaker.state is BreakerState.OPEN


def test_default_breaker_opens_under_total_outage(clock: FakeClock) -> None:
    breaker = CircuitBreaker(clock=clock)  # defaults: window 100 / min 100 / 0.5
    for _ in range(100):
        breaker.record_success()  # a healthy history fills the window first
    failures = 0
    while breaker.state is BreakerState.CLOSED:
        breaker.record_failure()
        failures += 1
    assert failures == 50  # half of a 100-attempt window


def test_default_breaker_opens_from_cold_under_total_outage(clock: FakeClock) -> None:
    breaker = CircuitBreaker(clock=clock)
    failures = 0
    while breaker.state is BreakerState.CLOSED:
        breaker.record_failure()
        failures += 1
    assert failures == 100  # needs one full window of evidence


def test_cold_start_failure_burst_does_not_open(clock: FakeClock) -> None:
    """Regression: instant 503s land before slow successes right after startup (or after
    the window is cleared). With a 20-attempt minimum this opened the breaker at a true
    20% failure rate; a full-window requirement must ride it out."""
    for seed in range(200):
        breaker = CircuitBreaker(clock=clock)
        for _ in range(12):  # the burst of instant failures from 50 concurrent calls
            breaker.record_failure()
        rng = random.Random(seed)
        for _ in range(500):
            if rng.random() < 0.2:
                breaker.record_failure()
            else:
                breaker.record_success()
        assert breaker.state is BreakerState.CLOSED, f"seed {seed}"


def test_stays_closed_under_steady_20_percent_failures(clock: FakeClock) -> None:
    """No flapping: 30,000 attempts at the normal 20% failure rate, across several seeds."""
    for seed in range(5):
        seen, hook = transitions_recorder()
        breaker = CircuitBreaker(clock=clock, on_transition=hook)  # type: ignore[arg-type]
        rng = random.Random(seed)
        for _ in range(30_000):
            if rng.random() < 0.2:
                breaker.record_failure()
            else:
                breaker.record_success()
        assert breaker.state is BreakerState.CLOSED, f"seed {seed}"
        assert seen == [], f"seed {seed} flapped: {seen}"


def open_breaker(clock: FakeClock, **kw: object) -> CircuitBreaker:
    breaker = CircuitBreaker(clock=clock, **kw)  # type: ignore[arg-type]
    for _ in range(100):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    return breaker


def test_half_open_after_cool_off_and_success_closes(clock: FakeClock) -> None:
    seen, hook = transitions_recorder()
    breaker = open_breaker(clock, open_s=10, on_transition=hook)
    clock.advance(9.9)
    assert breaker.state is BreakerState.OPEN
    clock.advance(0.2)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failure_ratio == 0.0  # window cleared on close
    assert seen == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]


def test_half_open_reopens_once_every_probe_has_failed(clock: FakeClock) -> None:
    breaker = open_breaker(clock, open_s=10, half_open_max=3)
    clock.advance(10)
    for _ in range(3):
        assert breaker.allow()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()


def test_fast_failed_probe_does_not_beat_a_slower_successful_one(clock: FakeClock) -> None:
    """Regression: a baseline 503 returns in ~5ms, a success in ~300ms. Re-opening on the
    first failed probe kept a recovered dependency behind the breaker for 7 rounds."""
    seen, hook = transitions_recorder()
    breaker = open_breaker(clock, open_s=10, half_open_max=3, on_transition=hook)
    clock.advance(10)
    for _ in range(3):
        assert breaker.allow()
    breaker.record_failure()  # the instant 503 lands first
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert seen[-2:] == [("open", "half_open"), ("half_open", "closed")]


def test_late_successful_probe_closes_a_long_lingering_half_open(clock: FakeClock) -> None:
    """Regression: the lost-probe safeguard measured from entering HALF_OPEN and ran inside
    record_success, so a probe issued long after that re-opened the breaker as it succeeded
    and the success was then discarded; DLQ replay aborted 149 of 150 recoverable calls."""
    breaker = open_breaker(clock, open_s=10, half_open_max=3)
    clock.advance(10)
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    clock.advance(30)  # traffic stops; HALF_OPEN lingers with one slot left
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_probe_failures_do_not_carry_over_between_rounds(clock: FakeClock) -> None:
    breaker = open_breaker(clock, open_s=10, half_open_max=3)
    clock.advance(10)
    for _ in range(3):
        breaker.allow()
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.advance(10)
    assert breaker.allow()
    breaker.record_failure()  # 1 of 3 in the new round, not 4 overall
    assert breaker.state is BreakerState.HALF_OPEN


def test_half_open_limits_concurrent_probes(clock: FakeClock) -> None:
    breaker = open_breaker(clock, open_s=10, half_open_max=3)
    clock.advance(10)
    assert [breaker.allow() for _ in range(5)] == [True, True, True, False, False]


def test_half_open_with_lost_probes_reopens_instead_of_stalling(clock: FakeClock) -> None:
    breaker = open_breaker(clock, open_s=10, half_open_max=3)
    clock.advance(10)
    for _ in range(3):
        assert breaker.allow()  # probes issued but never report back
    clock.advance(10)
    assert breaker.state is BreakerState.OPEN
    clock.advance(10)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()


async def test_fails_fast_while_open_without_calling_pipeline(clock: FakeClock) -> None:
    breaker = open_breaker(clock)
    pipeline = FakePipeline()
    try:
        await call_with_retry(
            lambda: pipeline.process("e1", "synthetic"),
            policy=RetryPolicy(),
            breaker=breaker,
            sleep=clock.sleep,
            clock=clock,
            rng=random.Random(0),
        )
    except RetryGiveUpError as exc:
        assert exc.reason == "circuit_open"
        assert exc.attempts == 0
    else:
        raise AssertionError("expected circuit_open")
    assert pipeline.calls == []


def test_transitions_are_logged_as_json(clock: FakeClock, caplog) -> None:
    caplog.set_level("INFO", logger="steadfast.breaker")
    open_breaker(clock)
    record = next(r for r in caplog.records if r.name == "steadfast.breaker")
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["event"] == "breaker_transition"
    assert fields["from"] == "closed"
    assert fields["to"] == "open"
    assert "at" in fields


async def test_retries_feed_the_breaker_window(clock: FakeClock) -> None:
    breaker = CircuitBreaker(window=20, min_calls=20, clock=clock)
    pipeline = FakePipeline(default=r503())
    for _ in range(7):
        with contextlib.suppress(RetryGiveUpError):
            await call_with_retry(
                lambda: pipeline.process("e", "synthetic"),
                policy=RetryPolicy(),
                breaker=breaker,
                sleep=clock.sleep,
                clock=clock,
                rng=random.Random(0),
            )
    # Each retry is an observation: the 20th failed attempt (call 7, attempt 2) reaches
    # min_calls and opens the circuit, so call 7's third attempt is refused.
    assert breaker.state is BreakerState.OPEN
    assert len(pipeline.calls) == 20
