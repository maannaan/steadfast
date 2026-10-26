"""Measure the model tier's per-stage latency with single, sequential calls.

Why sequential: the goal is the latency of one turn on an idle model (the number the
timeouts are derived from), not throughput. Queueing under load is measured separately by
the Phase E load test; here the `queue` stage should stay near zero.

Two views are collected:
- client side: POST /calls wall time, outcome and retry count for every call (includes
  injected faults and their retries);
- model side: the orchestrator's `model_turn` log events (one per successful attempt,
  with queue/stt/llm/tts milliseconds), aggregated with CloudWatch Logs Insights.

Usage: STEADFAST_URL=... STEADFAST_API_KEY=... model_latency.py --calls 40 --region R
Writes results/model-latency-<UTC>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx

QUERY = """
filter event = "model_turn"
| stats count() as turns,
  pct(latency_ms, 50) as attempt_p50, pct(latency_ms, 99) as attempt_p99,
  pct(queue_ms, 50) as queue_p50, pct(queue_ms, 99) as queue_p99,
  pct(stt_ms, 50) as stt_p50, pct(stt_ms, 99) as stt_p99,
  pct(llm_ms, 50) as llm_p50, pct(llm_ms, 99) as llm_p99,
  pct(tts_ms, 50) as tts_p50, pct(tts_ms, 99) as tts_p99
"""


def pct(values: list[float], q: float) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def run_calls(n: int) -> list[dict]:
    url = os.environ["STEADFAST_URL"]
    headers = {"x-steadfast-key": os.environ["STEADFAST_API_KEY"]}
    verify: str | bool = os.environ.get("STEADFAST_CA_BUNDLE") or True
    run = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    rows = []
    with httpx.Client(base_url=url, headers=headers, verify=verify, timeout=90) as client:
        for i in range(n):
            event = {
                "event_id": f"latency-{run}-{i:03d}",
                "campaign_id": "CAMP-LATENCY",
                "borrower_bucket": "31-60 DPD",
                "product_type": "personal_loan",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            start = time.perf_counter()
            resp = client.post("/calls", json=event)
            wall_ms = round((time.perf_counter() - start) * 1000)
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            rows.append(
                {
                    "event_id": event["event_id"],
                    "http": resp.status_code,
                    "wall_ms": wall_ms,
                    "outcome": body.get("outcome"),
                    "status": body.get("status"),
                    "retry_count": body.get("retry_count"),
                }
            )
            print(
                f"[{i + 1}/{n}] {resp.status_code} {body.get('outcome')} "
                f"retries={body.get('retry_count')} {wall_ms} ms",
                flush=True,
            )
    return rows


def query_stages(region: str, log_group: str, start: float, end: float) -> dict:
    logs = boto3.client("logs", region_name=region)
    qid = logs.start_query(
        logGroupName=log_group,
        startTime=int(start) - 60,
        endTime=int(end) + 60,
        queryString=QUERY,
    )["queryId"]
    while True:
        out = logs.get_query_results(queryId=qid)
        if out["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    if not out["results"]:
        return {}
    return {f["field"]: float(f["value"]) for f in out["results"][0]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calls", type=int, default=40)
    parser.add_argument("--region", required=True)
    parser.add_argument("--log-group", default="/ecs/steadfast/orchestrator")
    args = parser.parse_args()

    start = time.time()
    rows = run_calls(args.calls)
    end = time.time()
    print("waiting 30 s for logs to be indexed...", flush=True)
    time.sleep(30)
    stages = query_stages(args.region, args.log_group, start, end)

    ok = [r["wall_ms"] for r in rows if r["status"] == "succeeded"]
    summary = {
        "calls": len(rows),
        "succeeded": len(ok),
        "outcomes": {o: sum(r["outcome"] == o for r in rows) for o in {r["outcome"] for r in rows}},
        "retries_total": sum(r["retry_count"] or 0 for r in rows),
        "client_wall_ms_succeeded": {
            "p50": pct(ok, 50),
            "p99": pct(ok, 99),
            "max": max(ok, default=0),
        },
        "model_turn_ms": stages,
    }
    out = Path("results") / f"model-latency-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "calls": rows}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
