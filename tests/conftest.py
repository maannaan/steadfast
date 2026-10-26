"""Shared test doubles: a controllable clock, a sleep that advances it, a scripted pipeline."""

from collections.abc import Callable

import pytest

from service.models import CallEvent, PipelineResult
from service.pipeline.base import NonRetryableError, RetryableError
from service.store.db import Database

OK = PipelineResult(
    status="ok", transcript="synthetic transcript", response_audio_ref="synthetic", latency_ms=5
)


class FakeClock:
    """Monotonic clock that only moves when told to."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakePipeline:
    """Pipeline whose responses are scripted: each item is a result, an exception, or a
    zero-arg callable producing either. When the script runs out, `default` is used."""

    def __init__(
        self,
        script: list[PipelineResult | Exception | Callable[[], object]] | None = None,
        default: PipelineResult | Exception = OK,
    ) -> None:
        self.script = list(script or [])
        self.default = default
        self.calls: list[str] = []
        self.healthy = True

    async def process(self, event_id: str, audio_ref: str) -> PipelineResult:
        self.calls.append(event_id)
        item = self.script.pop(0) if self.script else self.default
        if callable(item) and not isinstance(item, PipelineResult):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    async def health(self) -> bool:
        return self.healthy


def r503() -> RetryableError:
    return RetryableError("http_503:downstream_timeout")


def r400() -> NonRetryableError:
    return NonRetryableError("http_400:bad_request")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.open()
    yield database
    database.close()


def make_event(event_id: str = "evt-1", **overrides: object) -> CallEvent:
    data: dict[str, object] = {
        "event_id": event_id,
        "campaign_id": "CAMP-2026-09-A",
        "borrower_bucket": "31-60 DPD",
        "product_type": "credit_card",
        "timestamp": "2026-09-01T09:04:01Z",
        **overrides,
    }
    return CallEvent.model_validate(data)
