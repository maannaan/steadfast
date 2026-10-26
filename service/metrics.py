"""In-process metrics rendered in the Prometheus text format on GET /metrics.

Hand-rolled (about 100 lines) instead of pulling in prometheus_client: the service needs a
handful of counters, gauges and one histogram, and keeping it in-repo makes the Round 2
switch to CloudWatch Embedded Metric Format a small, local change.
"""

from collections import defaultdict
from collections.abc import Iterable

from service.resilience.breaker import STATE_VALUE, BreakerState

Labels = tuple[tuple[str, str], ...]

# End-to-end call latency buckets in milliseconds (mock p50~300, deadline 8s).
LATENCY_BUCKETS_MS = (50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 10000)


def _labels(**labels: str) -> Labels:
    return tuple(sorted(labels.items()))


def _fmt_labels(labels: Labels, extra: Iterable[tuple[str, str]] = ()) -> str:
    items = [*labels, *extra]
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


class Counter:
    def __init__(self, name: str, help_text: str) -> None:
        self.name, self.help = name, help_text
        self.values: dict[Labels, float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self.values[_labels(**labels)] += amount

    def total(self) -> float:
        return sum(self.values.values())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        if not self.values:
            lines.append(f"{self.name} 0")
        lines += [f"{self.name}{_fmt_labels(k)} {v:g}" for k, v in sorted(self.values.items())]
        return lines


class Gauge:
    def __init__(self, name: str, help_text: str) -> None:
        self.name, self.help = name, help_text
        self.value = 0.0

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount

    def render(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} gauge",
            f"{self.name} {self.value:g}",
        ]


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...]) -> None:
        self.name, self.help = name, help_text
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.sum += value
        self.count += 1
        for i, upper in enumerate(self.buckets):
            if value <= upper:
                self.counts[i] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for upper, n in zip(self.buckets, self.counts, strict=True):
            lines.append(f'{self.name}_bucket{{le="{upper:g}"}} {n}')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self.count}')
        lines.append(f"{self.name}_sum {self.sum:g}")
        lines.append(f"{self.name}_count {self.count}")
        return lines


class Metrics:
    def __init__(self) -> None:
        p = "steadfast_"
        self.calls_total = Counter(p + "calls_total", "Calls handled, by final outcome.")
        self.attempts_total = Counter(p + "attempts_total", "Pipeline attempts made.")
        self.retries_total = Counter(
            p + "retries_total", "Pipeline retries (attempts after the first)."
        )
        self.downstream_errors_total = Counter(
            p + "downstream_errors_total", "Failed pipeline attempts, by error kind."
        )
        self.dlq_enqueued_total = Counter(
            p + "dlq_enqueued_total", "Calls written to the DLQ, by reason category."
        )
        self.replays_total = Counter(p + "replays_total", "DLQ replays, by result.")
        self.persistence_errors_total = Counter(
            p + "persistence_errors_total", "Failures writing call_log or DLQ (answered 500)."
        )
        self.duplicates_total = Counter(
            p + "duplicates_total", "Requests answered from an existing call_log row."
        )
        self.breaker_transitions_total = Counter(
            p + "breaker_transitions_total", "Circuit breaker state transitions."
        )
        self.in_flight = Gauge(p + "in_flight_calls", "Calls currently being processed.")
        self.breaker_state = Gauge(
            p + "breaker_state", "Circuit breaker state: 0 closed, 1 half_open, 2 open."
        )
        self.latency_ms = Histogram(
            p + "call_latency_ms", "End-to-end call handling latency (ms).", LATENCY_BUCKETS_MS
        )

    def on_breaker_transition(self, old: BreakerState, new: BreakerState) -> None:
        self.breaker_transitions_total.inc(**{"from": old.value, "to": new.value})
        self.breaker_state.set(STATE_VALUE[new])

    def render(self) -> str:
        metrics = [
            self.calls_total,
            self.attempts_total,
            self.retries_total,
            self.downstream_errors_total,
            self.dlq_enqueued_total,
            self.replays_total,
            self.persistence_errors_total,
            self.duplicates_total,
            self.breaker_transitions_total,
            self.in_flight,
            self.breaker_state,
            self.latency_ms,
        ]
        return "\n".join(line for m in metrics for line in m.render()) + "\n"
