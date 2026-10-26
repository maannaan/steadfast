"""EMF output shape: CloudWatch silently drops metrics from malformed `_aws` blocks, so the
structure is pinned here against the spec (every declared metric and dimension must be a
top-level member of the same JSON object)."""

import asyncio
import json
import logging
import random

import pytest

from service.logging_setup import JsonFormatter
from service.metrics import Metrics
from service.metrics_emf import NAMESPACE, EmfEmitter, publish_gauges
from service.resilience.breaker import CircuitBreaker
from service.resilience.executor import CallExecutor
from service.resilience.retry import RetryPolicy
from service.store.call_log import SqliteCallLog
from service.store.db import Database
from service.store.dlq import SqliteDLQ
from tests.conftest import OK, FakeClock, FakePipeline, make_event, r503


def assert_valid_emf(line: dict) -> None:
    aws = line["_aws"]
    assert isinstance(aws["Timestamp"], int)
    (directive,) = aws["CloudWatchMetrics"]
    assert directive["Namespace"] == NAMESPACE
    for dims in directive["Dimensions"]:
        for d in dims:
            assert isinstance(line[d], str)
    for metric in directive["Metrics"]:
        assert isinstance(line[metric["Name"]], int | float)


def test_call_fields_shape() -> None:
    fields = EmfEmitter("svc", "task1", clock=lambda: 1.5).call_fields(
        retries=2, downstream_errors=3, dlq=True, latency_ms=900
    )
    assert_valid_emf(fields)
    assert fields["_aws"]["Timestamp"] == 1500
    assert fields["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["ServiceName"]]
    assert (fields["Calls"], fields["Retries"], fields["DLQEnqueued"]) == (1, 2, 1)
    assert "TaskId" not in fields  # per-task dimensions only on gauges (metric cost)


def test_gauge_fields_have_task_dimension() -> None:
    fields = EmfEmitter("svc", "task1").gauge_fields(in_flight=7, circuit_open=True)
    assert_valid_emf(fields)
    dims = fields["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
    assert dims == [["ServiceName"], ["ServiceName", "TaskId"]]
    assert (fields["InFlightCalls"], fields["CircuitOpen"], fields["TaskId"]) == (7, 1, "task1")


async def test_call_log_line_carries_emf(
    db: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    executor = CallExecutor(
        pipeline=FakePipeline([r503(), OK]),
        breaker=CircuitBreaker(clock=clock),
        call_log=SqliteCallLog(db),
        dlq=SqliteDLQ(db),
        metrics=Metrics(),
        policy=RetryPolicy(),
        emf=EmfEmitter("steadfast-orchestrator", "t1"),
        sleep=clock.sleep,
        clock=clock,
        rng=random.Random(0),
    )
    with caplog.at_level(logging.INFO, logger="steadfast.calls"):
        await executor.handle_call(make_event())
    (record,) = [r for r in caplog.records if r.getMessage() == "call"]
    line = json.loads(JsonFormatter().format(record))
    assert_valid_emf(line)
    assert line["event"] == "call" and line["event_id"] == "evt-1"
    assert (line["Retries"], line["DownstreamErrors"], line["DLQEnqueued"]) == (1, 1, 0)


async def test_gauge_publisher_emits_on_a_fixed_cadence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with (
        caplog.at_level(logging.INFO, logger="steadfast.metrics"),
        pytest.raises(asyncio.CancelledError),
    ):
        await publish_gauges(EmfEmitter("svc", "t"), lambda: (4, False), 10, fake_sleep)
    lines = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert sleeps == [10, 10, 10]
    assert len(lines) == 3 and all(line["InFlightCalls"] == 4 for line in lines)
    for line in lines:
        assert_valid_emf(line)
