"""Pipeline implementation that speaks the Appendix B HTTP contract.

Works against the mock and against any model server exposing the same
POST /pipeline/process contract (the Round 2 GPU container does).
"""

import httpx
from pydantic import ValidationError

from service.models import PipelineResult
from service.pipeline.base import NonRetryableError, RetryableError


class HttpMockPipeline:
    """Calls POST {base_url}/pipeline/process over a shared, pooled httpx.AsyncClient.

    The client is injected rather than created here so one connection pool is shared by
    all requests (no per-call TCP handshakes) and tests can inject an in-process transport.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, timeout_s: float) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        try:
            response = await self._client.post(
                f"{self._base_url}/pipeline/process",
                json={"event_id": event_id, "audio_ref": audio_ref},
                timeout=self._timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise RetryableError("timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableError(f"transport_error:{type(exc).__name__}") from exc

        code = response.status_code
        # 5xx and 429 mean "the dependency is struggling right now" - worth another try.
        if code >= 500 or code == 429:
            raise RetryableError(f"http_{code}:{_reason(response)}")
        # Any other 4xx means our request is wrong; sending it again will not fix it.
        if code >= 400:
            raise NonRetryableError(f"http_{code}:{_reason(response)}")
        try:
            return PipelineResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise NonRetryableError("invalid_payload") from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"{self._base_url}/health", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("reason", "unknown"))
    except (ValueError, AttributeError):
        return "unknown"
