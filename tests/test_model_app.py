"""The model server's HTTP contract, with fake stages (no model weights needed).

The point is compatibility: the orchestrator talks to this server with the same client it
uses for the mock, so every response here must map onto RetryableError/NonRetryableError
the way the resilience layer expects.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from model.app import create_app
from model.settings import ModelSettings
from service.pipeline.base import NonRetryableError, RetryableError
from service.pipeline.open_weight import OpenWeightPipeline


class FakeStages:
    def __init__(self, tmp: Path, transcript: str = "I will pay on Friday", delay_s: float = 0):
        self.tmp, self.transcript, self.delay_s = tmp, transcript, delay_s

    def transcribe(self, wav_path: Path) -> str:
        time.sleep(self.delay_s)
        return self.transcript

    def reply(self, transcript: str) -> str:
        return "Thank you, I have noted Friday."

    def synthesize(self, text: str) -> Path:
        out = self.tmp / f"{time.monotonic_ns()}.wav"
        out.write_bytes(b"RIFF" + b"\0" * 40)
        return out


async def started(app) -> None:  # type: ignore[no-untyped-def]
    for _ in range(200):
        if app.state.engine.ready:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("model never became ready")


def make(tmp: Path, stages: FakeStages | None = None, gate: threading.Event | None = None, **kw):  # type: ignore[no-untyped-def]
    sample = tmp / "sample-0.wav"
    sample.write_bytes(b"RIFF")
    stages = stages or FakeStages(tmp)

    def loader(_: ModelSettings):  # type: ignore[no-untyped-def]
        if gate:
            gate.wait(5)
        return stages, [sample]

    return create_app(ModelSettings(fault_rate=0.0, **kw), loader=loader)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = make(tmp_path)
    async with app.router.lifespan_context(app):
        await started(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://model"
        ) as c:
            yield c


async def test_success_body_extends_the_mock_contract(client: httpx.AsyncClient) -> None:
    r = await client.post("/pipeline/process", json={"event_id": "e1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["transcript"] == "I will pay on Friday"
    assert body["reply"].startswith("Thank you")
    assert set(body["stage_ms"]) == {"queue", "stt", "llm", "tts"}
    assert body["latency_ms"] >= 0 and body["response_audio_ref"].startswith("tts:")


async def test_health_is_503_until_warm(tmp_path: Path) -> None:
    gate = threading.Event()
    app = make(tmp_path, gate=gate)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as c:
            assert (await c.get("/health")).status_code == 503
            r = await c.post("/pipeline/process", json={"event_id": "e1"})
            assert (r.status_code, r.json()["reason"]) == (503, "warming_up")
            gate.set()
            await started(app)
            assert (await c.get("/health")).status_code == 200


async def test_fault_injection_returns_the_spec_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = make(tmp_path)
    app.state.engine.settings.fault_rate = 1.0
    async with app.router.lifespan_context(app):
        await started(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as c:
            with caplog.at_level(logging.WARNING, logger="model"):
                r = await c.post("/pipeline/process", json={"event_id": "e1"})
    assert r.status_code == 503 and r.json() == {
        "status": "error",
        "reason": "downstream_timeout",
    }
    messages = [rec.getMessage() for rec in caplog.records]
    rejected = [json.loads(m) for m in messages if "model_rejected" in m]
    assert rejected == [
        {"event": "model_rejected", "event_id": "e1", "status": 503, "reason": "downstream_timeout"}
    ]


async def test_full_queue_is_a_fast_429(tmp_path: Path) -> None:
    app = make(tmp_path, FakeStages(tmp_path, delay_s=0.3), max_concurrency=1, max_queue=0)
    async with app.router.lifespan_context(app):
        await started(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://m") as c:
            first = asyncio.create_task(c.post("/pipeline/process", json={"event_id": "a"}))
            await asyncio.sleep(0.05)
            second = await c.post("/pipeline/process", json={"event_id": "b"})
            assert (second.status_code, second.json()["reason"]) == (429, "overloaded")
            assert (await first).status_code == 200


async def test_orchestrator_client_maps_model_responses(tmp_path: Path) -> None:
    app = make(tmp_path, FakeStages(tmp_path, transcript=""))
    async with app.router.lifespan_context(app):
        await started(app)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as c:
            pipeline = OpenWeightPipeline(c, "http://model", timeout_s=5)
            assert await pipeline.health()
            with pytest.raises(NonRetryableError, match="empty_transcript"):
                await pipeline.process("e1", "synthetic")
            app.state.engine.settings.fault_rate = 1.0
            with pytest.raises(RetryableError, match="downstream_timeout"):
                await pipeline.process("e1", "synthetic")


async def test_orchestrator_client_reads_stage_timings(client: httpx.AsyncClient) -> None:
    pipeline = OpenWeightPipeline(client, "http://model", timeout_s=5)
    result = await pipeline.process("e1", "sample-0")
    assert result.transcript == "I will pay on Friday"
    assert result.stage_ms is not None and set(result.stage_ms) >= {"stt", "llm", "tts"}
