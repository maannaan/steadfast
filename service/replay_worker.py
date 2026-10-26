"""Replay worker: drains the SQS dead-letter queue through the normal executor.

    python -m service.replay_worker [--exit-when-empty]

On AWS it runs as its own ECS service (desired count 0, scaled to 1 by
scripts/replay_aws.sh), not inside the request-serving tasks: a long replay must not hold
an HTTP request open past the load balancer's timeout, and must not compete with live
calls for a serving task's CPU. It is a service rather than a one-off `run-task` because
only service tasks get Service Connect, i.e. can reach http://mock:8081 / http://model:8080.

Per batch of messages it reuses replay_dead_letters, so the rate limit and circuit
breaker gating are exactly those of the /dlq/replay endpoint. Then, per message:
- entry replayed (now, or earlier by someone else)  -> delete the message;
- replay attempted and failed (replay_failed)      -> keep it: it reappears after the
  queue's visibility timeout (5 min), a natural backoff;
- not attempted (a probe showed the dependency still down) -> release it now and back
  off before polling again;
- no index entry (the index write failed after the send) -> replay from the payload the
  message carries; the executor records the outcome either way, so delete.
"""

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from service.config import Settings
from service.logging_setup import configure_logging, log_event
from service.metrics import Metrics
from service.models import CallEvent
from service.replay import replay_dead_letters
from service.resilience.executor import CallExecutor, PersistenceError
from service.runtime import runtime
from service.store.sqs_dlq import QueuedEvent, SqsDLQ

log = logging.getLogger("steadfast.replay_worker")

BACKOFF_S = 30.0  # after a probe showed the dependency still down


@dataclass
class WorkerStats:
    batches: int = 0
    replayed: int = 0
    failed: int = 0
    released: int = 0
    duplicates_deleted: int = 0
    recovered_from_payload: int = 0


async def run_worker(
    executor: CallExecutor,
    dlq: SqsDLQ,
    metrics: Metrics,
    *,
    rate_per_s: float,
    stop: asyncio.Event,
    exit_when_empty: bool = False,
    wait_s: int = 20,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> WorkerStats:
    stats = WorkerStats()
    while not stop.is_set():
        try:
            messages = await dlq.receive(wait_s=wait_s)
            if not messages:
                if exit_when_empty:
                    break
                continue
            aborted = await _process_batch(executor, dlq, metrics, messages, rate_per_s, stats)
        except (BotoCoreError, ClientError) as exc:
            # Transient AWS trouble (timeouts, throttling). Nothing is lost: undeleted
            # messages reappear after the visibility timeout, and replaying an event
            # twice is idempotent (it is already "replayed", so its message is deleted).
            log_event(log, "replay_worker_aws_error", logging.WARNING, error=repr(exc))
            await sleep(BACKOFF_S)
            continue
        log_event(log, "replay_batch", **asdict(stats), aborted=aborted)
        if aborted:
            await sleep(BACKOFF_S)
    return stats


async def run_workers(
    concurrency: int,
    executor: CallExecutor,
    dlq: SqsDLQ,
    metrics: Metrics,
    *,
    rate_per_s: float,
    stop: asyncio.Event,
    **kwargs: Any,
) -> WorkerStats:
    """Run `concurrency` receive loops side by side. A replayed call lasts as long as a live
    one (~27 s on the demo settings), and a loop waits for its whole batch of 10 before
    polling again, so one loop drains only ~0.4 calls/s. Loops share the executor (one
    breaker) and split the rate limit, so the total start rate stays `rate_per_s`."""
    results = await asyncio.gather(
        *(
            run_worker(
                executor, dlq, metrics, rate_per_s=rate_per_s / concurrency, stop=stop, **kwargs
            )
            for _ in range(concurrency)
        )
    )
    total = WorkerStats()
    for r in results:
        for field, value in asdict(r).items():
            setattr(total, field, getattr(total, field) + value)
    return total


async def _process_batch(
    executor: CallExecutor,
    dlq: SqsDLQ,
    metrics: Metrics,
    messages: list[QueuedEvent],
    rate_per_s: float,
    stats: WorkerStats,
) -> bool:
    """Replay one received batch and settle each message. Returns whether the replay was
    aborted because the dependency is still down."""
    stats.batches += 1
    first: dict[str, QueuedEvent] = {}
    for message in messages:
        if message.event_id in first:
            await dlq.delete(message)  # another pointer to the same event
            stats.duplicates_deleted += 1
        else:
            first[message.event_id] = message

    summary = await replay_dead_letters(
        executor, dlq, metrics, event_ids=list(first), rate_per_s=rate_per_s
    )
    for event_id, message in first.items():
        entry = await dlq.get(event_id)
        if entry is None:
            await _replay_from_payload(executor, dlq, message, stats)
        elif entry.status == "replayed":
            await dlq.delete(message)
            stats.replayed += 1
        elif entry.status == "pending":
            await dlq.release(message)
            stats.released += 1
        else:
            stats.failed += 1
    return summary.aborted


async def _replay_from_payload(
    executor: CallExecutor, dlq: SqsDLQ, message: QueuedEvent, stats: WorkerStats
) -> None:
    if not message.payload:
        log_event(log, "replay_orphan_message", logging.ERROR, event_id=message.event_id)
        return
    try:
        await executor.handle_call(CallEvent.model_validate(message.payload), replay=True)
    except PersistenceError:
        return  # nothing recorded: keep the message so it is retried
    await dlq.delete(message)
    stats.recovered_from_payload += 1


async def _main(exit_when_empty: bool) -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    if settings.dlq_backend != "sqs":
        log_event(
            log, "replay_worker_misconfigured", logging.ERROR, dlq_backend=settings.dlq_backend
        )
        return 2

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with runtime(settings) as rt:
        assert isinstance(rt.stores.dlq, SqsDLQ)
        log_event(
            log,
            "replay_worker_started",
            rate_per_s=settings.replay_rate_per_s,
            concurrency=settings.replay_concurrency,
        )
        stats = await run_workers(
            settings.replay_concurrency,
            rt.executor,
            rt.stores.dlq,
            rt.metrics,
            rate_per_s=settings.replay_rate_per_s,
            stop=stop,
            exit_when_empty=exit_when_empty,
        )
        await rt.executor.drain(settings.shutdown_drain_s)
    log_event(log, "replay_worker_stopped", **asdict(stats))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain the SQS dead-letter queue.")
    parser.add_argument(
        "--exit-when-empty", action="store_true", help="stop after an empty long poll"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.exit_when_empty)))


if __name__ == "__main__":
    main()
