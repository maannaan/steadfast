"""The "plug socket" between the resilience layer and any STT -> LLM -> TTS implementation.

The resilience layer (retry, breaker, executor) only ever sees this protocol and the two
exception types below. That is what makes Steadfast model-agnostic: swapping the mock for a
real open-weight stack means writing one class that maps its own failures onto
RetryableError / NonRetryableError, with no change to retry or DLQ logic.
"""

from typing import Protocol, runtime_checkable

from service.models import PipelineResult


class PipelineError(Exception):
    """Base for pipeline failures. `attempts` is filled in by the retry layer."""

    attempts: int = 0


class RetryableError(PipelineError):
    """Transient failure worth retrying: 5xx, 429, timeouts, connection errors, GPU OOM."""


class NonRetryableError(PipelineError):
    """Retrying cannot help: other 4xx, undecodable audio, malformed responses."""


@runtime_checkable
class Pipeline(Protocol):
    async def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        """Run one STT -> LLM -> TTS turn. Raises RetryableError or NonRetryableError."""
        ...

    async def health(self) -> bool:
        """True if the pipeline is reachable and ready to serve."""
        ...
