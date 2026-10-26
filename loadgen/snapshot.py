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


async def take_snapshot(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    stats = (await client.get(f"{url}/stats", timeout=10)).json()
    metrics = (await client.get(f"{url}/metrics", timeout=10)).text
    return {"stats": stats, "breaker_transitions": parse_transitions(metrics)}
