"""Assemble the executor and its dependencies from settings.

Shared by the HTTP service (service/main.py) and the replay worker
(service/replay_worker.py) so both process calls through exactly the same retry, breaker
and persistence configuration.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from service.config import Settings
from service.metrics import Metrics
from service.metrics_emf import EmfEmitter, discover_task_id
from service.pipeline.base import Pipeline
from service.pipeline.factory import build_pipeline
from service.resilience.breaker import CircuitBreaker
from service.resilience.executor import CallExecutor
from service.resilience.retry import RetryPolicy
from service.store.factory import Stores, build_stores


@dataclass
class Runtime:
    settings: Settings
    stores: Stores
    metrics: Metrics
    breaker: CircuitBreaker
    executor: CallExecutor
    emf: EmfEmitter | None


@asynccontextmanager
async def runtime(
    settings: Settings,
    *,
    pipeline: Pipeline | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[Runtime]:
    stores = build_stores(settings)
    limits = httpx.Limits(
        max_connections=settings.http_max_connections,
        max_keepalive_connections=settings.http_max_connections,
    )
    client = http_client or httpx.AsyncClient(limits=limits)
    try:
        metrics = Metrics()
        breaker = CircuitBreaker(
            window=settings.breaker_window,
            min_calls=settings.breaker_min_calls,
            failure_threshold=settings.breaker_failure_threshold,
            open_s=settings.breaker_open_s,
            half_open_max=settings.breaker_half_open_max,
            on_transition=metrics.on_breaker_transition,
        )
        emf = (
            EmfEmitter(settings.service_name, await discover_task_id(client))
            if settings.metrics_backend == "emf"
            else None
        )
        executor = CallExecutor(
            pipeline=pipeline or build_pipeline(settings, client),
            breaker=breaker,
            call_log=stores.call_log,
            dlq=stores.dlq,
            metrics=metrics,
            policy=RetryPolicy(
                max_attempts=settings.max_attempts,
                base_s=settings.backoff_base_s,
                cap_s=settings.backoff_cap_s,
                attempt_timeout_s=settings.attempt_timeout_s,
                deadline_s=settings.call_deadline_s,
            ),
            turns_per_call=settings.turns_per_call,
            turn_gap_s=settings.turn_gap_s,
            outcome_seed=settings.outcome_seed,
            emf=emf,
        )
        yield Runtime(settings, stores, metrics, breaker, executor, emf)
    finally:
        if http_client is None:
            await client.aclose()
        stores.close()
