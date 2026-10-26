"""HttpMockPipeline maps HTTP outcomes onto the two error types, and the mock meets spec."""

import httpx
import pytest

from mock.app import MockSettings, create_app, in_burst
from service.pipeline.base import NonRetryableError, RetryableError
from service.pipeline.http_mock import HttpMockPipeline


def pipeline_for(handler) -> HttpMockPipeline:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpMockPipeline(client, "http://pipe", timeout_s=1.0)


@pytest.mark.parametrize("code", [500, 502, 503, 504, 429])
async def test_server_errors_are_retryable(code: int) -> None:
    pipe = pipeline_for(lambda req: httpx.Response(code, json={"reason": "downstream_timeout"}))
    with pytest.raises(RetryableError, match=f"http_{code}"):
        await pipe.process("e1", "synthetic")


@pytest.mark.parametrize("code", [400, 404, 422])
async def test_client_errors_are_not_retryable(code: int) -> None:
    pipe = pipeline_for(lambda req: httpx.Response(code, json={}))
    with pytest.raises(NonRetryableError):
        await pipe.process("e1", "synthetic")


async def test_invalid_payload_is_not_retryable() -> None:
    pipe = pipeline_for(lambda req: httpx.Response(200, json={"status": "weird"}))
    with pytest.raises(NonRetryableError, match="invalid_payload"):
        await pipe.process("e1", "synthetic")


async def test_connection_errors_and_timeouts_are_retryable() -> None:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(RetryableError, match="transport_error"):
        await pipeline_for(refuse).process("e1", "synthetic")
    with pytest.raises(RetryableError, match="timeout"):
        await pipeline_for(slow).process("e1", "synthetic")


async def test_against_real_mock_contract() -> None:
    mock = create_app(MockSettings(failure_rate=0.0, latency_mu=0.0, latency_sigma=0.1))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
    pipe = HttpMockPipeline(client, "http://mock", timeout_s=1.0)
    result = await pipe.process("e1", "synthetic")
    assert result.status == "ok"
    assert result.transcript == "synthetic transcript"
    assert await pipe.health()


async def test_mock_failure_rate_is_close_to_20_percent() -> None:
    mock = create_app(MockSettings(seed=123, latency_mu=-5.0, latency_sigma=0.1))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock), base_url="http://mock"
    ) as client:
        codes = [
            (await client.post("/pipeline/process", json={"event_id": f"e{i}"})).status_code
            for i in range(4000)
        ]
    rate = codes.count(503) / len(codes)
    assert 0.18 <= rate <= 0.22, rate


def test_burst_schedule() -> None:
    # 45s healthy, then 15s of total failure, repeating.
    assert not in_burst(0, on_s=15, off_s=45)
    assert not in_burst(44.9, on_s=15, off_s=45)
    assert in_burst(45, on_s=15, off_s=45)
    assert in_burst(59.9, on_s=15, off_s=45)
    assert not in_burst(60, on_s=15, off_s=45)
