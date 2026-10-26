"""Client for the open-weight model server (model/app.py): STT -> LLM -> TTS over HTTP.

Why HTTP and not in-process: the models need a GPU (or a large CPU host) and scale on their
own schedule, while the orchestrator is a small Fargate task that scales with call volume.
Keeping them apart behind the same POST /pipeline/process contract as the mock means the
resilience layer (retry, breaker, DLQ) is unchanged; swapping is PIPELINE_BACKEND plus
PIPELINE_URL.

The only differences from the mock client: the model returns per-stage timings
(`stage_ms`), logged here so slow stages are visible from the orchestrator's side, and a
turn takes seconds, so ATTEMPT_TIMEOUT_S / CALL_DEADLINE_S are set from measured p99s
(see docs/aws/model-tier.md) rather than the mock's sub-second values.
"""

import logging

from service.logging_setup import log_event
from service.models import PipelineResult
from service.pipeline.http_mock import HttpMockPipeline

log = logging.getLogger("steadfast.pipeline")


class OpenWeightPipeline(HttpMockPipeline):
    async def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        result = await super().process(event_id, audio_ref)
        if result.stage_ms:
            log_event(
                log,
                "model_turn",
                event_id=event_id,
                latency_ms=result.latency_ms,
                **{f"{k}_ms": v for k, v in result.stage_ms.items()},
            )
        return result
