"""Retry with exponential backoff, full jitter, a per-attempt timeout and a call deadline.

Why full jitter (sleep = uniform(0, min(cap, base * 2**n))): when the dependency hiccups,
many calls fail at the same moment. Fixed backoff makes them all retry at the same moment
too, hitting the dependency in synchronised waves. Full jitter spreads retries evenly across
the backoff interval (the AWS Architecture Blog "Exponential Backoff and Jitter" result).

Why a deadline as well as max attempts: a borrower is on the line. Attempt count alone
does not bound wall-clock time when attempts are slow, so we also stop once the next sleep
would push us past CALL_DEADLINE_S.

The breaker is consulted before every attempt, so a call that is mid-retry stops as soon as
the circuit opens instead of hammering a dependency that is known to be down.

Sleep, clock and RNG are injectable so tests run instantly and deterministically.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from service.pipeline.base import NonRetryableError, RetryableError
from service.resilience.breaker import CircuitBreaker

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
AttemptHook = Callable[[Exception | None], None]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_s: float = 0.2
    cap_s: float = 2.0
    attempt_timeout_s: float = 2.5
    deadline_s: float = 8.0


class RetryGiveUpError(RetryableError):
    """Raised when we stop trying. `reason` is the DLQ reason recorded for the call.

    reason is one of: "retries_exhausted", "deadline_exceeded", "circuit_open".
    Subclasses RetryableError because the underlying failure was transient.
    """

    def __init__(self, reason: str, attempts: int, last_error: str | None) -> None:
        super().__init__(f"{reason} after {attempts} attempt(s): {last_error}")
        self.reason = reason
        self.attempts = attempts
        self.last_error = last_error


def backoff_delay(retry_number: int, base_s: float, cap_s: float, rng: random.Random) -> float:
    """Full-jitter delay before retry `retry_number` (0 = first retry)."""
    return rng.uniform(0, min(cap_s, base_s * 2**retry_number))


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    breaker: CircuitBreaker,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
    rng: random.Random | None = None,
    on_attempt: AttemptHook | None = None,
) -> tuple[T, int]:
    """Run `fn` until it succeeds or we give up. Returns (result, retries_used).

    Raises RetryGiveUpError when giving up on transient failures, or re-raises
    NonRetryableError immediately (with `.attempts` set). Every attempt's result is recorded
    in the breaker and reported to `on_attempt` (None = success) for metrics.
    """
    rng = rng or random.Random()
    start = clock()
    last_error: str | None = None

    for attempt in range(policy.max_attempts):
        if attempt > 0:
            delay = backoff_delay(attempt - 1, policy.base_s, policy.cap_s, rng)
            if clock() - start + delay >= policy.deadline_s:
                raise RetryGiveUpError("deadline_exceeded", attempt, last_error)
            await sleep(delay)

        if not breaker.allow():
            raise RetryGiveUpError("circuit_open", attempt, last_error)

        # Never let one attempt run past the overall deadline.
        timeout = min(policy.attempt_timeout_s, policy.deadline_s - (clock() - start))
        if timeout <= 0:
            raise RetryGiveUpError("deadline_exceeded", attempt, last_error)

        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
        except NonRetryableError as exc:
            # The dependency answered; our request was the problem. That says nothing bad
            # about dependency health, so it counts as a success for the breaker.
            breaker.record_success()
            if on_attempt:
                on_attempt(exc)
            exc.attempts = attempt + 1
            raise
        except (RetryableError, TimeoutError) as exc:
            breaker.record_failure()
            if on_attempt:
                on_attempt(exc)
            last_error = str(exc) or "attempt_timeout"
            continue

        breaker.record_success()
        if on_attempt:
            on_attempt(None)
        return result, attempt

    raise RetryGiveUpError("retries_exhausted", policy.max_attempts, last_error)
