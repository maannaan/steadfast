"""End to end: the real mock and the real service wired together in-process.

No fakes in the call path: the service talks HTTP (via httpx's ASGI transport) to the
Appendix B mock, writes to a real SQLite file, and we verify the outcome through the
service's own API exactly as the load report does.
"""

import asyncio
import random
from datetime import UTC, datetime

import httpx
import pytest

from loadgen.events import make_event
from loadgen.snapshot import parse_transitions
from mock.app import MockSettings
from mock.app import create_app as create_mock
from service.config import Settings
from service.main import create_app as create_service


async def fire(client: httpx.AsyncClient, events: list[dict], concurrency: int = 50) -> list[int]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(event: dict) -> int:
        async with semaphore:
            return (await client.post("/calls", json=event)).status_code

    return await asyncio.gather(*(one(e) for e in events))


def events(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    ts = datetime(2026, 9, 1, 9, tzinfo=UTC)
    return [make_event(rng, "CAMP-2026-09-A", ts) for _ in range(n)]


async def unaccounted(client: httpx.AsyncClient, sent: list[dict]) -> dict:
    ids = [e["event_id"] for e in sent]
    return (await client.post("/calls/lookup", json={"event_ids": ids})).json()


@pytest.fixture
def stack(tmp_path):
    """Build (mock_app, service_app, client) with a fast-latency mock."""

    def build(mock_settings: MockSettings, **service_overrides: object):
        mock = create_mock(mock_settings)
        to_mock = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock))
        settings = Settings(
            db_path=str(tmp_path / "e2e.db"),
            pipeline_url="http://mock",
            outcome_seed=1,
            **service_overrides,  # type: ignore[arg-type]
        )
        service = create_service(settings, http_client=to_mock, setup_logging=False)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="http://service"
        )
        return mock, service, client

    return build


async def test_500_events_at_20_percent_failure(stack) -> None:
    # Fast latency (~1ms) keeps the test quick; failure behaviour is exactly Appendix B.
    _, service, client = stack(MockSettings(failure_rate=0.2, seed=2026, latency_mu=0.0))
    sent = events(500, seed=1)
    async with service.router.lifespan_context(service), client:
        codes = await fire(client, sent)
        lookup = await unaccounted(client, sent)
        stats = (await client.get("/stats")).json()
        metrics = (await client.get("/metrics")).text

    assert codes.count(200) == 500
    assert lookup["missing"] == []  # zero unaccounted calls
    assert lookup["in_call_log"] == 500
    dlq_rate = lookup["in_dlq"] / 500
    assert dlq_rate < 0.03, f"DLQ rate {dlq_rate:.1%}"
    assert stats["succeeded_after_retry"] > 0  # retries actually happened
    assert parse_transitions(metrics) == {}  # breaker never opened at the normal 20%


async def test_outage_opens_breaker_then_replay_recovers_everything(stack) -> None:
    mock, service, client = stack(
        MockSettings(failure_rate=1.0, seed=7, latency_mu=0.0),
        breaker_open_s=0.3,  # short cool-off so the test does not wait 10s
    )
    sent = events(150, seed=2)
    async with service.router.lifespan_context(service), client:
        codes = await fire(client, sent, concurrency=10)
        reasons = {
            item["reason"]
            for item in (await client.get("/dlq", params={"limit": 1000})).json()["items"]
        }
        transitions = parse_transitions((await client.get("/metrics")).text)
        during = await unaccounted(client, sent)

        # The dependency recovers; give the breaker time to go half-open, then replay.
        mock.state.settings.failure_rate = 0.0
        await asyncio.sleep(0.4)
        replay = (await client.post("/dlq/replay", json={"all": True, "rate_per_s": 1000})).json()
        stats = (await client.get("/stats")).json()
        after = await unaccounted(client, sent)

    assert codes.count(200) == 150  # every call answered and recorded, even in the outage
    assert during["missing"] == []
    assert "circuit_open" in reasons
    assert transitions.get("closed->open", 0) >= 1

    assert replay["failed"] == 0
    assert replay["replayed"] == 150
    assert stats["dlq_pending"] == 0
    assert stats["dlq_replay_failed"] == 0
    assert stats["failed_downstream"] == 0  # every call_log row flipped to a real outcome
    assert after["missing"] == []
