"""Open-weight STT -> LLM -> TTS server with the same contract as the mock.

POST /pipeline/process {"event_id": "...", "audio_ref": "synthetic" | "sample-N"}
  200 {"status": "ok", "transcript", "reply", "response_audio_ref", "latency_ms",
       "stage_ms": {"queue", "stt", "llm", "tts"}}
  503 {"status": "error", "reason": "downstream_timeout"}   injected fault (FAULT_RATE)
  503 {"status": "error", "reason": "warming_up"}           models still loading
  429 {"status": "error", "reason": "overloaded"}           queue full, try again
  503 {"status": "error", "reason": "queue_timeout"}        waited too long for a slot
  503 {"status": "error", "reason": "model_error"}          inference failed
  422 {"status": "error", "reason": "empty_transcript"}     nothing intelligible
GET /health: 200 once every stage is loaded and has served one warm-up turn, else 503,
so neither ECS nor the orchestrator sends real calls to a cold model.

The orchestrator's existing HTTP client needs no change: 5xx/429 are retryable, 4xx is
not, and the extra fields are ignored by older clients.
"""

import asyncio
import json
import logging
import os
import random
import time
import zlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from model.settings import ModelSettings
from model.stages import Stages, timed

log = logging.getLogger("model")


class ProcessRequest(BaseModel):
    event_id: str = Field(min_length=1)
    audio_ref: str = "synthetic"


def error(status: int, reason: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"status": "error", "reason": reason})


class Engine:
    """Owns the stages and the admission control. `load` runs in a thread at startup."""

    def __init__(self, settings: ModelSettings) -> None:
        self.settings = settings
        self.stages: Stages | None = None
        self.samples: list[Path] = []
        self.slots = asyncio.Semaphore(settings.max_concurrency)
        self.waiting = 0
        self.ready = False

    def sample_for(self, event_id: str, audio_ref: str) -> Path:
        """`sample-N` picks a clip; anything else picks one from the event_id, so the
        same call always hears the same borrower line."""
        if audio_ref.startswith("sample-") and audio_ref[7:].isdigit():
            return self.samples[int(audio_ref[7:]) % len(self.samples)]
        return self.samples[zlib.crc32(event_id.encode()) % len(self.samples)]

    def run_turn(self, wav: Path) -> dict:
        assert self.stages is not None
        transcript, stt_ms = timed(self.stages.transcribe, wav)
        if not transcript:
            return {"transcript": "", "stage_ms": {"stt": stt_ms}}
        reply, llm_ms = timed(self.stages.reply, transcript)
        audio, tts_ms = timed(self.stages.synthesize, reply or "Thank you.")
        audio_bytes = audio.stat().st_size
        audio.unlink(missing_ok=True)  # nothing downstream consumes the audio in this demo
        return {
            "transcript": transcript,
            "reply": reply,
            "response_audio_ref": f"tts:{audio_bytes}B",
            "stage_ms": {"stt": stt_ms, "llm": llm_ms, "tts": tts_ms},
        }


def load_real_stages(settings: ModelSettings) -> tuple[Stages, list[Path]]:
    """Blocking: fetch weights, load every model, synthesise the input clips, warm up."""
    from model import weights
    from model.stages import LlamaLlm, PiperTts, RealStages, WhisperStt, make_samples

    root = Path(settings.models_dir)
    if settings.weights_bucket:
        n = weights.fetch(settings.weights_bucket, settings.weights_prefix, str(root))
        log.info("weights ready (%d downloaded)", n)
    tts = PiperTts(settings.piper_bin, root / settings.voice_file, Path(settings.tts_dir))
    stt = WhisperStt(
        root / settings.whisper_dir, settings.whisper_device, settings.whisper_compute_type
    )
    llm = LlamaLlm(
        root / settings.llm_file,
        settings.llm_gpu_layers,
        settings.llm_threads,
        settings.llm_context,
        settings.max_tokens,
    )
    stages = RealStages(stt, llm, tts)
    samples = make_samples(tts, Path(settings.samples_dir))
    return stages, samples


def create_app(
    settings: ModelSettings | None = None,
    loader: Callable[[ModelSettings], tuple[Stages, list[Path]]] = load_real_stages,
) -> FastAPI:
    settings = settings or ModelSettings()
    engine = Engine(settings)
    rng = random.Random(settings.seed)

    async def warm_up() -> None:
        started = time.perf_counter()
        try:
            engine.stages, engine.samples = await asyncio.to_thread(loader, settings)
            # One full turn so the first real call does not pay first-inference costs.
            turn = await asyncio.to_thread(engine.run_turn, engine.samples[0])
        except Exception:
            # A model that cannot load will never get healthy: exit so ECS replaces the
            # task now rather than after the health-check grace period.
            log.exception("model warm-up failed")
            os._exit(1)
        engine.ready = True
        log.info(
            json.dumps(
                {
                    "event": "model_ready",
                    "load_s": round(time.perf_counter() - started, 1),
                    "warmup": turn,
                }
            )
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(warm_up())
        yield
        task.cancel()

    app = FastAPI(title="Steadfast model pipeline", lifespan=lifespan)
    app.state.engine = engine

    @app.post("/pipeline/process")
    async def process(body: ProcessRequest, request: Request) -> JSONResponse:
        if not engine.ready:
            return error(503, "warming_up")
        if rng.random() < settings.fault_rate:
            return error(503, "downstream_timeout")
        if engine.waiting >= settings.max_queue + settings.max_concurrency:
            return error(429, "overloaded")

        started = time.perf_counter()
        engine.waiting += 1
        try:
            try:
                await asyncio.wait_for(engine.slots.acquire(), settings.queue_timeout_s)
            except TimeoutError:
                return error(503, "queue_timeout")
            queue_ms = round((time.perf_counter() - started) * 1000)
            try:
                wav = engine.sample_for(body.event_id, body.audio_ref)
                turn = await asyncio.to_thread(engine.run_turn, wav)
            except Exception as exc:
                log.exception("inference failed for %s", body.event_id)
                return error(503, f"model_error:{type(exc).__name__}")
            finally:
                engine.slots.release()
        finally:
            engine.waiting -= 1

        if not turn["transcript"]:
            return error(422, "empty_transcript")
        latency_ms = round((time.perf_counter() - started) * 1000)
        turn["stage_ms"]["queue"] = queue_ms
        log.info(
            json.dumps(
                {
                    "event": "turn",
                    "event_id": body.event_id,
                    "latency_ms": latency_ms,
                    "stage_ms": turn["stage_ms"],
                }
            )
        )
        return JSONResponse(content={"status": "ok", "latency_ms": latency_ms, **turn})

    @app.get("/health")
    async def health() -> JSONResponse:
        if not engine.ready:
            return JSONResponse(status_code=503, content={"status": "warming_up"})
        return JSONResponse(content={"status": "ok"})

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8080, access_log=False, timeout_keep_alive=75)


if __name__ == "__main__":
    main()
