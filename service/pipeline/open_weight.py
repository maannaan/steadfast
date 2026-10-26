"""Placeholder for an in-process open-weight STT -> LLM -> TTS pipeline (Round 2).

Not implemented in Round 1. It exists to show exactly where a real model stack plugs in:
it implements the same Pipeline protocol, so retry, breaker and DLQ code stay untouched.

Planned stack (all licences compatible with an Apache-2.0 release; verify before shipping):
- STT: faster-whisper large-v3-turbo (CTranslate2, MIT), compute_type int8_float16.
- LLM: Mistral-7B-Instruct-v0.3 (Apache-2.0) served by vLLM's OpenAI-compatible endpoint,
  short collections-agent system prompt, max_tokens ~80.
- TTS: Piper (CPU), one English and one Hindi voice.
"""

from service.models import PipelineResult


class OpenWeightPipeline:
    """Runs STT, LLM and TTS against real open-weight models.

    TODO (Round 2):
    - __init__: load faster-whisper, create an httpx client for the vLLM endpoint, load
      Piper voices. Warm up each stage once at startup so the first real call does not
      pay model-load latency; health() returns False until warm-up completes.
    - process():
        1. resolve audio_ref to a WAV file and transcribe it (faster-whisper),
        2. send the transcript to the LLM with the system prompt,
        3. synthesise the reply with Piper and store it, returning its reference,
        4. return PipelineResult with total latency (plus per-stage timings).
    - Error mapping (the only contract the resilience layer relies on):
        * CUDA out-of-memory, vLLM 429/503 (overloaded), timeouts -> RetryableError
        * undecodable or empty audio, prompt rejected by the model  -> NonRetryableError
    - Concurrency: bound in-flight GPU work with a semaphore sized to measured capacity, so
      overload turns into fast RetryableErrors instead of a growing queue.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "PIPELINE_BACKEND=open_weight is not implemented in Round 1. Use "
            "PIPELINE_BACKEND=mock, or point PIPELINE_URL at any model server that speaks "
            "the POST /pipeline/process contract. See docs/MODEL_SWAP.md."
        )

    async def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        raise NotImplementedError

    async def health(self) -> bool:
        raise NotImplementedError
