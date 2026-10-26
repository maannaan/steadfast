"""The executor: the one code path every call goes through, live or replayed.

The guarantee it enforces is "zero unaccounted calls": a call either ends with a call_log
row (and, if it failed, a DLQ entry), or the caller gets an error (PersistenceError -> HTTP
500) and knows it was not recorded. There is no path where we answer 200 for a call that
was not written down.

Ordering on failure: the DLQ entry is written BEFORE the call_log row. If the second write
fails, the event is still in the DLQ (replayable) and the API answers 500; the reverse order
could leave a failed call that nobody can replay.

Duplicates: a request whose event_id is already in call_log gets the stored result without
touching the pipeline. Concurrent requests with the same event_id share one in-flight task,
so the pipeline is called once even if both arrive at the same moment.
"""

import asyncio
import logging
import random
import time
from datetime import UTC, datetime

from service.logging_setup import log_event
from service.metrics import Metrics
from service.models import CallEvent, CallResult, Outcome
from service.pipeline.base import NonRetryableError, Pipeline
from service.resilience.breaker import BreakerState, CircuitBreaker
from service.resilience.retry import (
    Clock,
    RetryGiveUpError,
    RetryPolicy,
    Sleep,
    call_with_retry,
)
from service.store.call_log import CallLog
from service.store.dlq import DLQBackend

log = logging.getLogger("steadfast.calls")

AUDIO_REF = "synthetic"

# Business outcome mix for calls that completed, per Appendix A (~60/20/15/5).
BUSINESS_OUTCOMES = (Outcome.CONNECTED, Outcome.NO_ANSWER, Outcome.PTP_TAKEN, Outcome.DISPUTE)
OUTCOME_WEIGHTS = (60, 20, 15, 5)


class PersistenceError(Exception):
    """The call could not be recorded. The API must answer 500, never 200."""


def sample_outcome(event_id: str, seed: int | None) -> tuple[Outcome, int]:
    """Pick a business outcome and call duration for a successfully handled call.

    Seeded per event (seed + event_id) rather than from one shared RNG, so the result is
    reproducible regardless of the order concurrent calls finish in, and a replay of the
    same event produces the same outcome.
    """
    rng = random.Random(f"{seed}:{event_id}") if seed is not None else random.Random()
    outcome = rng.choices(BUSINESS_OUTCOMES, weights=OUTCOME_WEIGHTS)[0]
    duration = 0 if outcome is Outcome.NO_ANSWER else rng.randint(30, 120)
    return outcome, duration


def error_kind(error: Exception) -> str:
    """Low-cardinality label for a failed attempt, e.g. http_503, timeout, transport_error.

    Separating "the dependency said 503" from "we timed out waiting" is the first question
    in the runbook: is the problem downstream, or in our own service?
    """
    if isinstance(error, TimeoutError):
        return "attempt_timeout"
    return (str(error).split(":", 1)[0] or type(error).__name__)[:32]


class CallExecutor:
    def __init__(
        self,
        *,
        pipeline: Pipeline,
        breaker: CircuitBreaker,
        call_log: CallLog,
        dlq: DLQBackend,
        metrics: Metrics,
        policy: RetryPolicy,
        turns_per_call: int = 1,
        turn_gap_s: float = 0.0,
        outcome_seed: int | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._breaker = breaker
        self._call_log = call_log
        self._dlq = dlq
        self._metrics = metrics
        self._policy = policy
        self._turns = turns_per_call
        self._turn_gap_s = turn_gap_s
        self._outcome_seed = outcome_seed
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._inflight: dict[str, asyncio.Task[CallResult]] = {}

    @property
    def breaker_state(self) -> BreakerState:
        return self._breaker.state

    async def handle_call(self, event: CallEvent, *, replay: bool = False) -> CallResult:
        """Handle one call event end to end. Raises PersistenceError if it cannot be recorded.

        replay=True (used by DLQ replay) re-runs events whose stored outcome is
        failed_downstream instead of returning the stored failure.

        The work runs in its own task, shielded from the caller: if the HTTP client
        disconnects mid-call, the call still finishes and is persisted.
        """
        task = self._inflight.get(event.event_id)
        if task is None:
            task = asyncio.create_task(self._handle(event, replay))
            self._inflight[event.event_id] = task
            task.add_done_callback(lambda t, eid=event.event_id: self._forget(eid, t))
        return await asyncio.shield(task)

    def _forget(self, event_id: str, task: asyncio.Task[CallResult]) -> None:
        if self._inflight.get(event_id) is task:
            del self._inflight[event_id]

    async def drain(self, timeout_s: float) -> int:
        """Wait for in-flight calls to finish (graceful shutdown). Returns how many did not."""
        tasks = list(self._inflight.values())
        if not tasks:
            return 0
        _, pending = await asyncio.wait(tasks, timeout=timeout_s)
        return len(pending)

    async def _handle(self, event: CallEvent, replay: bool) -> CallResult:
        try:
            existing = await self._call_log.get(event.event_id)
        except Exception as exc:
            self._metrics.persistence_errors_total.inc()
            raise PersistenceError("call_log read failed") from exc
        if existing and not (replay and existing.outcome is Outcome.FAILED_DOWNSTREAM):
            self._metrics.duplicates_total.inc()
            return existing

        self._metrics.in_flight.inc()
        try:
            return await self._process(event, replay)
        finally:
            self._metrics.in_flight.dec()

    async def _process(self, event: CallEvent, replay: bool) -> CallResult:
        received_at = datetime.now(UTC)
        start = self._clock()
        retries = 0
        failure: tuple[str, int, str | None] | None = None  # (dlq reason, attempts, last error)

        try:
            for turn in range(self._turns):
                if turn and self._turn_gap_s:
                    await self._sleep(self._turn_gap_s)
                _, used = await call_with_retry(
                    lambda: self._pipeline.process(event.event_id, AUDIO_REF),
                    policy=self._policy,
                    breaker=self._breaker,
                    sleep=self._sleep,
                    clock=self._clock,
                    rng=self._rng,
                    on_attempt=self._on_attempt,
                )
                retries += used
        except RetryGiveUpError as exc:
            retries += max(exc.attempts - 1, 0)
            failure = (exc.reason, exc.attempts, exc.last_error)
        except NonRetryableError as exc:
            retries += max(exc.attempts - 1, 0)
            failure = (f"non_retryable:{exc}", exc.attempts, str(exc))
        except Exception as exc:
            # A bug in a pipeline implementation must still leave a record, never a lost call.
            log.exception("unexpected pipeline error", extra={"fields": {"event": "error"}})
            failure = (f"internal_error:{type(exc).__name__}", 0, str(exc))

        latency_ms = round((self._clock() - start) * 1000)
        result = self._build_result(event, retries, latency_ms, failure)
        await self._persist(event, result, failure, received_at)
        self._record(result, replay)
        return result

    def _build_result(
        self,
        event: CallEvent,
        retries: int,
        latency_ms: int,
        failure: tuple[str, int, str | None] | None,
    ) -> CallResult:
        if failure is None:
            outcome, duration = sample_outcome(event.event_id, self._outcome_seed)
            status, reason = "succeeded", None
        else:
            outcome, duration = Outcome.FAILED_DOWNSTREAM, 0
            status, reason = "failed", failure[0]
        return CallResult(
            event_id=event.event_id,
            campaign_id=event.campaign_id,
            borrower_bucket=event.borrower_bucket,
            product_type=event.product_type,
            timestamp=event.timestamp,
            outcome=outcome,
            call_duration_s=duration,
            retry_count=retries,
            latency_ms=max(latency_ms, 0),
            status=status,
            dlq_reason=reason,
        )

    async def _persist(
        self,
        event: CallEvent,
        result: CallResult,
        failure: tuple[str, int, str | None] | None,
        received_at: datetime,
    ) -> None:
        try:
            if failure is not None:
                reason, attempts, last_error = failure
                payload = event.model_dump(mode="json", exclude_none=True)
                await self._dlq.enqueue(event.event_id, payload, reason, attempts, last_error)
            await self._call_log.upsert(result, received_at, datetime.now(UTC))
        except Exception as exc:
            self._metrics.persistence_errors_total.inc()
            log_event(
                log,
                "persistence_failed",
                logging.ERROR,
                event_id=event.event_id,
                error=type(exc).__name__,
            )
            raise PersistenceError(f"could not record call {event.event_id}") from exc

    def _on_attempt(self, error: Exception | None) -> None:
        self._metrics.attempts_total.inc()
        if error is not None:
            self._metrics.downstream_errors_total.inc(kind=error_kind(error))

    def _record(self, result: CallResult, replay: bool) -> None:
        m = self._metrics
        m.calls_total.inc(outcome=result.outcome.value)
        m.retries_total.inc(result.retry_count)
        m.latency_ms.observe(result.latency_ms)
        if result.dlq_reason:
            m.dlq_enqueued_total.inc(reason=result.dlq_reason.split(":", 1)[0])
        log_event(
            log,
            "call",
            event_id=result.event_id,
            outcome=result.outcome.value,
            retries=result.retry_count,
            latency_ms=result.latency_ms,
            breaker_state=self._breaker.state.value,
            replay=replay,
            dlq_reason=result.dlq_reason,
        )
