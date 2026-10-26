"""Mock STT -> LLM -> TTS pipeline, built to the exact Appendix B contract.

POST /pipeline/process {"event_id": "...", "audio_ref": "synthetic"}
- With probability FAILURE_RATE (default 0.2), independently per request:
  HTTP 503 {"status": "error", "reason": "downstream_timeout"}, returned immediately.
- Otherwise: sleep a lognormal latency, then return the success body.

Latency: the spec suggests lognormvariate(mu=5.7, sigma=0.35) for p50~300ms / p90~700ms /
p99~1200ms, but sigma=0.35 only gives p90~470ms. The default here is sigma=0.66 (p90~700ms);
scripts/check_latency.py shows the numbers. Both are env-configurable.

FAILURE_MODE=burst adds correlated outages: after BURST_OFF_S healthy seconds, every request
fails for BURST_ON_S seconds, repeating. That is what proves the circuit breaker opens.

Settings live on `app.state.settings` so in-process tests can change behaviour mid-run.
"""

import asyncio
import random
import time
from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    failure_rate: float = Field(default=0.2, ge=0, le=1)
    latency_mu: float = 5.7
    latency_sigma: float = Field(default=0.66, ge=0)
    seed: int | None = None
    failure_mode: Literal["independent", "burst"] = "independent"
    burst_on_s: float = Field(default=15.0, ge=0)
    burst_off_s: float = Field(default=45.0, ge=0)


class ProcessRequest(BaseModel):
    event_id: str = Field(min_length=1)
    audio_ref: str = "synthetic"


FAILURE_BODY = {"status": "error", "reason": "downstream_timeout"}


def in_burst(elapsed_s: float, on_s: float, off_s: float) -> bool:
    """True while inside a 100%-failure burst: healthy for off_s, then failing for on_s."""
    period = on_s + off_s
    if period <= 0 or on_s <= 0:
        return False
    return (elapsed_s % period) >= off_s


def create_app(
    settings: MockSettings | None = None, clock: Callable[[], float] = time.monotonic
) -> FastAPI:
    settings = settings or MockSettings()
    app = FastAPI(title="Steadfast mock pipeline")
    app.state.settings = settings
    app.state.rng = random.Random(settings.seed)
    app.state.clock = clock
    app.state.started_at = clock()

    def should_fail(request: Request) -> bool:
        s: MockSettings = request.app.state.settings
        if s.failure_mode == "burst":
            elapsed = request.app.state.clock() - request.app.state.started_at
            if in_burst(elapsed, s.burst_on_s, s.burst_off_s):
                return True
        return request.app.state.rng.random() < s.failure_rate

    @app.post("/pipeline/process")
    async def process(body: ProcessRequest, request: Request) -> JSONResponse:
        if should_fail(request):
            return JSONResponse(status_code=503, content=FAILURE_BODY)
        s: MockSettings = request.app.state.settings
        latency_ms = request.app.state.rng.lognormvariate(s.latency_mu, s.latency_sigma)
        await asyncio.sleep(latency_ms / 1000)
        return JSONResponse(
            content={
                "status": "ok",
                "transcript": "synthetic transcript",
                "response_audio_ref": "synthetic",
                "latency_ms": round(latency_ms),
            }
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
