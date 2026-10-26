"""Point-in-time snapshot of the service's /stats and breaker counters.

The generator snapshots before and after a run so the report can show what happened
DURING the run (e.g. breaker transitions), not totals since the service started.
"""

import re
from typing import Any

import httpx

TRANSITION_LINE = re.compile(
    r'^steadfast_breaker_transitions_total\{from="(\w+)",to="(\w+)"\} ([0-9.e+]+)$', re.M
)


def parse_transitions(metrics_text: str) -> dict[str, float]:
    return {f"{a}->{b}": float(v) for a, b, v in TRANSITION_LINE.findall(metrics_text)}


async def take_snapshot(
    client: httpx.AsyncClient, url: str, prom_metrics: bool = True
) -> dict[str, Any]:
    """prom_metrics=False behind a load balancer: /metrics is per process, so each scrape
    would hit a random task and before/after deltas would be meaningless. /stats still
    works there because it reads the shared call log. Breaker data then comes from
    CloudWatch instead."""
    stats = (await client.get(f"{url}/stats", timeout=30)).json()
    if not prom_metrics:
        return {"stats": stats, "breaker_transitions": None}
    metrics = (await client.get(f"{url}/metrics", timeout=10)).text
    return {"stats": stats, "breaker_transitions": parse_transitions(metrics)}
