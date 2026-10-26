"""HTTP API for the Steadfast call service.

POST /calls answers 200 for every call that was handled AND recorded, including calls whose
outcome is failed_downstream: "the dependency failed, and here is the record". It answers
500 only when the call could not be recorded, so a 200 always means "persisted".
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from service.config import Settings
from service.logging_setup import configure_logging, log_event
from service.metrics_emf import publish_gauges
from service.models import CallEvent, CallResult
from service.pipeline.base import Pipeline
from service.replay import replay_dead_letters
from service.resilience.breaker import BreakerState
from service.resilience.executor import PersistenceError
from service.runtime import runtime
from service.store.dlq import DLQStatus

log = logging.getLogger("steadfast.api")


class ReplayRequest(BaseModel):
    event_ids: list[str] | None = None
    all: bool = False
    rate_per_s: float = Field(default=5.0, gt=0, le=1000)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "ReplayRequest":
        if bool(self.event_ids) == self.all:
            raise ValueError('provide either "event_ids" or "all": true')
        return self


class LookupRequest(BaseModel):
    event_ids: list[str] = Field(max_length=10_000)


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: Pipeline | None = None,
    http_client: httpx.AsyncClient | None = None,
    setup_logging: bool = True,
) -> FastAPI:
    """Build the app. `pipeline` / `http_client` are injection points for tests."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if setup_logging:
            configure_logging(settings.log_level)
        async with runtime(settings, pipeline=pipeline, http_client=http_client) as rt:
            app.state.settings = settings
            app.state.metrics = rt.metrics
            app.state.breaker = rt.breaker
            app.state.call_log = rt.stores.call_log
            app.state.dlq = rt.stores.dlq
            app.state.executor = rt.executor
            gauges = None
            if rt.emf:
                gauges = asyncio.create_task(
                    publish_gauges(
                        rt.emf,
                        lambda: (
                            rt.metrics.in_flight.value,
                            rt.breaker.state is BreakerState.OPEN,
                        ),
                        settings.gauge_interval_s,
                    )
                )
            log_event(
                log,
                "service_started",
                pipeline_backend=settings.pipeline_backend,
                pipeline_url=settings.pipeline_url,
                call_log_backend=settings.call_log_backend,
                dlq_backend=settings.dlq_backend,
                metrics_backend=settings.metrics_backend,
            )
            try:
                yield
            finally:
                undrained = await rt.executor.drain(settings.shutdown_drain_s)
                if gauges:
                    gauges.cancel()
                log_event(log, "service_stopped", undrained_calls=undrained)

    app = FastAPI(title="Steadfast call service", lifespan=lifespan)

    @app.post("/calls", response_model=CallResult)
    async def post_call(event: CallEvent, request: Request) -> Any:
        try:
            return await request.app.state.executor.handle_call(event)
        except PersistenceError:
            return JSONResponse(
                status_code=500,
                content={"detail": "persistence_failed", "event_id": event.event_id},
            )

    @app.get("/calls/{event_id}", response_model=CallResult)
    async def get_call(event_id: str, request: Request) -> Any:
        result = await request.app.state.call_log.get(event_id)
        if result is None:
            raise HTTPException(status_code=404, detail="not found")
        return result

    @app.post("/calls/lookup")
    async def lookup_calls(body: LookupRequest, request: Request) -> dict[str, Any]:
        """Which event_ids are recorded? The load report uses this to prove zero unaccounted."""
        ids = list(dict.fromkeys(body.event_ids))
        logged = await request.app.state.call_log.existing_ids(ids)
        in_dlq = await request.app.state.dlq.existing_ids(ids)
        missing = [i for i in ids if i not in logged]
        return {
            "requested": len(ids),
            "in_call_log": len(logged),
            "in_dlq": len(in_dlq),
            "missing": missing,
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: the process is up and serving HTTP."""
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        """Readiness: we can record calls. Deliberately NOT tied to pipeline health: if the
        pipeline is down we still accept calls and record them as failed (DLQ). Failing
        readiness then would make a load balancer drop every call on the floor instead."""
        log_ok = await request.app.state.call_log.healthy()
        pipeline_ok = getattr(request.app.state, "executor", None) is not None
        body = {"call_log_writable": log_ok, "pipeline_client": pipeline_ok}
        return JSONResponse(status_code=200 if log_ok and pipeline_ok else 503, content=body)

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            request.app.state.metrics.render(), media_type="text/plain; version=0.0.4"
        )

    @app.get("/dlq")
    async def list_dlq(
        request: Request,
        status: DLQStatus | None = None,
        reason: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        items = await request.app.state.dlq.list(status=status, reason=reason, limit=limit)
        return {"count": len(items), "items": [asdict(i) for i in items]}

    @app.get("/dlq/{event_id}")
    async def get_dlq(event_id: str, request: Request) -> dict[str, Any]:
        entry = await request.app.state.dlq.get(event_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="not found")
        return asdict(entry)

    @app.post("/dlq/replay")
    async def replay(body: ReplayRequest, request: Request) -> dict[str, Any]:
        state = request.app.state
        summary = await replay_dead_letters(
            state.executor,
            state.dlq,
            state.metrics,
            event_ids=body.event_ids,
            replay_all=body.all,
            rate_per_s=body.rate_per_s,
        )
        return asdict(summary)

    @app.get("/stats")
    async def stats(request: Request) -> dict[str, Any]:
        state = request.app.state
        calls = await state.call_log.stats()
        dlq = await state.dlq.stats()
        return {
            **calls,
            "dlq_pending": dlq["pending"],
            "dlq_replayed": dlq["replayed"],
            "dlq_replay_failed": dlq["replay_failed"],
            "dlq_total": dlq["total"],
            "breaker_state": state.breaker.state.value,
            "in_flight": state.metrics.in_flight.value,
        }

    return app


app = create_app()
