"""CloudWatch metrics via Embedded Metric Format (EMF) log lines.

Why EMF: on ECS, stdout already goes to CloudWatch Logs. A JSON line with an `_aws` block
is turned into CloudWatch metrics by the Logs service itself, so the task needs no
cloudwatch:PutMetricData permission and no metrics agent, and the per-call line stays the
single source of truth (audit record and metric in one).

Two kinds of line:
- per call: Calls, Retries, DownstreamErrors, DLQEnqueued, LatencyMs, merged into the
  existing "call" log event. Dimension [ServiceName] only: a TaskId dimension here would
  create a new set of billed metrics for every task autoscaling ever starts.
- every PUBLISH_INTERVAL_S: InFlightCalls and CircuitOpen, with [ServiceName] (what
  autoscaling tracks: Average across tasks = in-flight per task) and [ServiceName, TaskId]
  (the per-task view on the dashboard).

Spec: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from service.logging_setup import log_event

NAMESPACE = "CollectionsChallenge"

log = logging.getLogger("steadfast.metrics")


class EmfEmitter:
    def __init__(
        self, service_name: str, task_id: str, clock: Callable[[], float] = time.time
    ) -> None:
        self.service_name = service_name
        self.task_id = task_id
        self._clock = clock

    def _block(self, dimensions: list[list[str]], metrics: dict[str, str]) -> dict[str, Any]:
        return {
            "Timestamp": int(self._clock() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": dimensions,
                    "Metrics": [{"Name": n, "Unit": u} for n, u in metrics.items()],
                }
            ],
        }

    def call_fields(
        self, *, retries: int, downstream_errors: int, dlq: bool, latency_ms: int
    ) -> dict[str, Any]:
        """Fields to merge into the per-call log line."""
        units = {
            "Calls": "Count",
            "Retries": "Count",
            "DownstreamErrors": "Count",
            "DLQEnqueued": "Count",
            "LatencyMs": "Milliseconds",
        }
        return {
            "_aws": self._block([["ServiceName"]], units),
            "ServiceName": self.service_name,
            "Calls": 1,
            "Retries": retries,
            "DownstreamErrors": downstream_errors,
            "DLQEnqueued": int(dlq),
            "LatencyMs": latency_ms,
        }

    def gauge_fields(self, *, in_flight: float, circuit_open: bool) -> dict[str, Any]:
        units = {"InFlightCalls": "Count", "CircuitOpen": "Count"}
        return {
            "_aws": self._block([["ServiceName"], ["ServiceName", "TaskId"]], units),
            "ServiceName": self.service_name,
            "TaskId": self.task_id,
            "InFlightCalls": in_flight,
            "CircuitOpen": int(circuit_open),
        }


async def publish_gauges(
    emitter: EmfEmitter,
    read: Callable[[], tuple[float, bool]],
    interval_s: float,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Emit the gauge line every interval_s until cancelled.

    A fixed cadence matters for autoscaling: with every task reporting every 10 s, the
    Average over a minute is the true mean in-flight per task. Publishing only on change
    would leave idle tasks silent and bias the average upwards.
    """
    while True:
        in_flight, circuit_open = read()
        log_event(
            log,
            "gauges",
            **emitter.gauge_fields(in_flight=in_flight, circuit_open=circuit_open),
        )
        await sleep(interval_s)


async def discover_task_id(client: httpx.AsyncClient) -> str:
    """Short ECS task ID from the task metadata endpoint, or the hostname elsewhere."""
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if uri:
        try:
            response = await client.get(f"{uri}/task", timeout=2.0)
            return str(response.json()["TaskARN"]).rsplit("/", 1)[-1]
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    return os.environ.get("HOSTNAME", "local")
