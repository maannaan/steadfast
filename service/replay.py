"""DLQ replay: resubmit dead-lettered calls through the normal executor path.

Replay deliberately reuses CallExecutor.handle_call (with replay=True) instead of a
separate code path, so replayed calls get the same retries, breaker protection and
persistence guarantees as live ones. A successful replay overwrites the call_log row
(failed_downstream -> real outcome) and marks the DLQ entry replayed.

Replay is rate-limited because the usual reason for a DLQ backlog is a dependency that
just recovered; flooding it with the whole backlog at once could knock it over again.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from pydantic import ValidationError

from service.logging_setup import log_event
from service.metrics import Metrics
from service.models import CallEvent, Outcome
from service.resilience.breaker import BreakerState
from service.resilience.executor import CallExecutor, PersistenceError
from service.resilience.retry import Sleep
from service.store.dlq import DLQBackend

log = logging.getLogger("steadfast.replay")

OPEN_POLL_S = 0.05


@dataclass
class ReplaySummary:
    requested: int = 0
    replayed: int = 0
    failed: int = 0
    skipped_already_replayed: int = 0
    # Not attempted because a probe showed the dependency still failing; still pending.
    aborted: int = 0
    missing: list[str] = field(default_factory=list)


async def replay_dead_letters(
    executor: CallExecutor,
    dlq: DLQBackend,
    metrics: Metrics,
    *,
    event_ids: list[str] | None = None,
    replay_all: bool = False,
    rate_per_s: float = 5.0,
    limit: int = 10_000,
    sleep: Sleep = asyncio.sleep,
) -> ReplaySummary:
    """Replay specific event_ids, or every entry not yet replayed (pending + replay_failed)."""
    if replay_all:
        entries = await dlq.list(status="pending", limit=limit)
        entries += await dlq.list(status="replay_failed", limit=limit)
        ids = [e.event_id for e in entries][:limit]
    else:
        ids = list(dict.fromkeys(event_ids or []))  # de-duplicate, keep order

    summary = ReplaySummary(requested=len(ids))

    async def replay_one(event_id: str) -> None:
        entry = await dlq.get(event_id)
        if entry is None:
            summary.missing.append(event_id)
            return
        if entry.status == "replayed":
            summary.skipped_already_replayed += 1
            return

        try:
            event = CallEvent.model_validate(entry.payload)
            result = await executor.handle_call(event, replay=True)
            ok = result.outcome is not Outcome.FAILED_DOWNSTREAM
        except (PersistenceError, ValidationError) as exc:
            log_event(log, "replay_error", logging.ERROR, event_id=event_id, error=repr(exc))
            ok = False

        if ok:
            await dlq.mark_replayed(event_id)
            summary.replayed += 1
            metrics.replays_total.inc(result="replayed")
        else:
            await dlq.mark_replay_failed(event_id)
            summary.failed += 1
            metrics.replays_total.inc(result="replay_failed")

    # rate_per_s paces when replays start, not when they finish: awaiting each one in turn
    # would cap throughput at 1 / pipeline latency regardless of the requested rate.
    # The breaker decides how far to fan out. Feeding entries into an OPEN breaker only
    # burns them as circuit_open, so wait out the cool-off; in HALF_OPEN the next replay is
    # the probe and must finish first. If the probe fails, the dependency is still down:
    # stop and leave the rest pending for a later replay.
    interval = 1.0 / rate_per_s
    tasks: list[asyncio.Task[None]] = []
    for i, event_id in enumerate(ids):
        if i:
            await sleep(interval)
        while executor.breaker_state is BreakerState.OPEN:
            await sleep(OPEN_POLL_S)
        task = asyncio.create_task(replay_one(event_id))
        tasks.append(task)
        if executor.breaker_state is BreakerState.HALF_OPEN:
            await task
            if executor.breaker_state is not BreakerState.CLOSED:
                summary.aborted = len(ids) - i - 1
                break
    await asyncio.gather(*tasks)

    log_event(
        log,
        "replay_finished",
        requested=summary.requested,
        replayed=summary.replayed,
        failed=summary.failed,
        aborted=summary.aborted,
        missing=len(summary.missing),
    )
    return summary
