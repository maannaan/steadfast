"""Summarise a load run and prove that no call went unaccounted.

UNACCOUNTED = events the generator sent that have no call_log row, checked against the
service itself (POST /calls/lookup), not against what the generator believes happened.
It must be 0; the report exits with code 1 otherwise, so CI and Make targets fail loudly.

Usage: python -m loadgen.report [--run results/run-<ts>.jsonl] [--url ...] [--label burst]
Writes results/run-<ts>.json next to the run file and prints a table.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from loadgen.auth import auth_headers, tls_verify
from loadgen.snapshot import parse_transitions

LOOKUP_CHUNK = 5000


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def latest_run(results_dir: Path) -> Path:
    runs = sorted(results_dir.glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not runs:
        sys.exit(f"no run-*.jsonl files in {results_dir}")
    return runs[-1]


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def transitions_delta(meta: dict[str, Any] | None, metrics_text: str) -> dict[str, float]:
    """Breaker transitions during the run if we have a before-snapshot, else since start."""
    now = parse_transitions(metrics_text)
    if not meta or meta["before"]["breaker_transitions"] is None:
        return now
    before = meta["before"]["breaker_transitions"]
    after = meta["after"]["breaker_transitions"]
    return {
        k: after.get(k, 0) - before.get(k, 0) for k in after if after.get(k, 0) != before.get(k, 0)
    }


async def build_report(
    run_path: Path, url: str, label: str | None, prom_metrics: bool = True
) -> dict[str, Any]:
    records = load_records(run_path)
    meta_path = run_path.parent / f"{run_path.stem}.meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    ids = [r["event_id"] for r in records]

    missing: list[str] = []
    in_dlq = 0
    async with httpx.AsyncClient(
        timeout=60.0, headers=auth_headers(), verify=tls_verify()
    ) as client:
        for i in range(0, len(ids), LOOKUP_CHUNK):
            response = await client.post(
                f"{url}/calls/lookup", json={"event_ids": ids[i : i + LOOKUP_CHUNK]}
            )
            response.raise_for_status()
            data = response.json()
            missing += data["missing"]
            in_dlq += data["in_dlq"]
        stats = (await client.get(f"{url}/stats")).json()
        transitions: dict[str, float] | None = None
        if prom_metrics:
            metrics_text = (await client.get(f"{url}/metrics")).text
            transitions = transitions_delta(meta, metrics_text)

    ok = [r for r in records if r.get("http_status") == 200]
    succeeded = [r for r in ok if r["outcome"] != "failed_downstream"]
    failed = [r for r in ok if r["outcome"] == "failed_downstream"]
    latencies = sorted(float(r["client_latency_ms"]) for r in ok)
    sent = len(records)

    return {
        "label": label,
        "run_file": run_path.name,
        "config": {k: meta[k] for k in ("speed", "start", "duration_hours", "seed")}
        if meta
        else None,
        "real_duration_s": meta["real_duration_s"] if meta else None,
        "sent": sent,
        "succeeded_first_try": sum(1 for r in succeeded if r["retry_count"] == 0),
        "succeeded_after_retry": sum(1 for r in succeeded if r["retry_count"] > 0),
        "failed_downstream": len(failed),
        "dlq_reasons": dict(Counter(r["dlq_reason"].split(":", 1)[0] for r in failed)),
        "dlq_count": in_dlq,
        "dlq_rate_pct": round(100 * in_dlq / sent, 2) if sent else 0.0,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        # None = not measured here (multi-task run: see the CloudWatch dashboard).
        "breaker_transitions": transitions,
        "breaker_opens": sum(v for k, v in transitions.items() if k.endswith("->open"))
        if transitions is not None
        else None,
        "http_errors": sent - len(ok),
        "unaccounted": len(missing),
        "unaccounted_sample": missing[:10],
        "service_stats_after": stats,
    }


def print_table(report: dict[str, Any]) -> None:
    lat = report["latency_ms"]
    rows = [
        ("scenario", report["label"] or "-"),
        ("sent", report["sent"]),
        ("succeeded first try", report["succeeded_first_try"]),
        ("succeeded after retry", report["succeeded_after_retry"]),
        ("failed_downstream", report["failed_downstream"]),
        ("DLQ count", report["dlq_count"]),
        ("DLQ rate", f"{report['dlq_rate_pct']}%"),
        ("DLQ reasons", report["dlq_reasons"] or "-"),
        ("latency p50 / p95 / p99 (ms)", f"{lat['p50']} / {lat['p95']} / {lat['p99']}"),
        (
            "breaker transitions",
            "n/a (per task; see CloudWatch)"
            if report["breaker_transitions"] is None
            else report["breaker_transitions"] or "none",
        ),
        ("HTTP errors", report["http_errors"]),
        ("UNACCOUNTED", report["unaccounted"]),
    ]
    width = max(len(name) for name, _ in rows)
    print()
    for name, value in rows:
        print(f"  {name:<{width}}  {value}")
    verdict = "PASS" if report["unaccounted"] == 0 else "FAIL"
    print(f"\n  {verdict}: {report['unaccounted']} unaccounted calls\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise a load run.")
    parser.add_argument("--run", type=Path, help="run-*.jsonl file (default: latest)")
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--url", default=os.environ.get("STEADFAST_URL", "http://localhost:8080"))
    parser.add_argument("--label", help="scenario name, e.g. independent, burst, outage")
    parser.add_argument(
        "--no-prom-metrics",
        action="store_true",
        help="skip per-process /metrics (use behind a load balancer)",
    )
    args = parser.parse_args()

    run_path = args.run or latest_run(args.results_dir)
    report = asyncio.run(
        build_report(run_path, args.url.rstrip("/"), args.label, not args.no_prom_metrics)
    )
    out_path = run_path.with_suffix(".json")
    out_path.write_text(json.dumps(report, indent=2))
    print_table(report)
    print(f"  saved {out_path}")
    sys.exit(0 if report["unaccounted"] == 0 else 1)


if __name__ == "__main__":
    main()
