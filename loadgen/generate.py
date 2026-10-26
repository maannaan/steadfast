"""Replay the Appendix C traffic profile against the service, with time compression.

--speed N compresses time: 1 simulated hour takes 60/N real minutes (--speed 60 runs a
24h day in 24 real minutes). The call RATE is compressed too, so a simulated day always
contains the same number of calls (~23,400): at --speed 60 the 50 calls/min peak becomes
50 calls per real second. That keeps the ramp and hold shape intact and makes the test
harder, not easier.

Arrivals are Poisson (exponential gaps), because real dialler traffic is bursty rather
than evenly spaced. A semaphore bounds concurrent requests (--concurrency, default 64) and
one pooled httpx client is shared. Every sent event is written to results/run-<ts>.jsonl;
before/after service snapshots go to run-<ts>.meta.json for the report.

Usage:
  python -m loadgen.generate --url http://localhost:8080 --speed 60 --start 07:45 \
      --duration-hours 2.5 --seed 42
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from loadgen.events import make_event
from loadgen.schedule import expected_calls, parse_hhmm, rate_per_min
from loadgen.snapshot import take_snapshot

PROGRESS_EVERY_S = 10.0
CLIENT_ATTEMPTS = 3


async def post_with_retry(
    client: httpx.AsyncClient, url: str, event: dict[str, Any]
) -> tuple[httpx.Response | None, str | None, int]:
    """POST a call, retrying transport failures (dropped connection, timeout).

    Safe because POST /calls is idempotent by event_id: if the first request did reach the
    service, the retry gets the stored result instead of a second call. This is how a
    dialler should talk to Steadfast; without it, a connection dropped before the request
    reached the service would be an unaccounted call on the CLIENT side.
    """
    error = None
    for attempt in range(1, CLIENT_ATTEMPTS + 1):
        try:
            return await client.post(f"{url}/calls", json=event), None, attempt
        except httpx.TransportError as exc:
            error = type(exc).__name__
            await asyncio.sleep(0.2 * attempt)
    return None, error, CLIENT_ATTEMPTS


def _hhmm(sim_minute: float) -> str:
    minute = int(sim_minute) % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


async def run_load(
    *,
    url: str,
    speed: float,
    start_minute: float,
    duration_hours: float,
    seed: int | None,
    campaign_id: str,
    out_dir: Path,
    concurrency: int,
    sim_date: date,
) -> Path:
    rng = random.Random(seed)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    run_path = out_dir / f"run-{stamp}.jsonl"
    meta_path = out_dir / f"run-{stamp}.meta.json"
    duration_min = duration_hours * 60
    sim_origin = datetime(sim_date.year, sim_date.month, sim_date.day, tzinfo=UTC)

    print(
        f"target {url} | speed x{speed:g} | sim {_hhmm(start_minute)} for {duration_hours:g}h "
        f"(~{duration_min * 60 / speed:.0f}s real) | expect ~"
        f"{expected_calls(start_minute, duration_min):.0f} calls"
    )

    semaphore = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    tasks: set[asyncio.Task[None]] = set()
    counts = {"sent": 0, "done": 0, "http_errors": 0}

    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        try:
            before = await take_snapshot(client, url)
        except httpx.HTTPError as exc:
            sys.exit(f"service not reachable at {url}: {exc}")

        with run_path.open("w") as out:

            async def send(event: dict[str, Any], sim_minute: float) -> None:
                async with semaphore:
                    record: dict[str, Any] = {
                        "event_id": event["event_id"],
                        "sent_at": datetime.now(UTC).isoformat(),
                        "sim_time": _hhmm(sim_minute),
                    }
                    started = time.monotonic()
                    response, error, attempts = await post_with_retry(client, url, event)
                    record["client_attempts"] = attempts
                    if response is None:
                        record["http_status"] = None
                        record["error"] = error
                        counts["http_errors"] += 1
                    else:
                        record["http_status"] = response.status_code
                        if response.status_code == 200:
                            body = response.json()
                            record["outcome"] = body["outcome"]
                            record["retry_count"] = body["retry_count"]
                            record["service_latency_ms"] = body["latency_ms"]
                            record["dlq_reason"] = body.get("dlq_reason")
                        else:
                            counts["http_errors"] += 1
                    record["client_latency_ms"] = round((time.monotonic() - started) * 1000)
                    out.write(json.dumps(record) + "\n")
                    counts["done"] += 1

            loop_start = time.monotonic()
            last_progress = loop_start
            next_at = loop_start + rng.expovariate(rate_per_min(start_minute) * speed / 60)
            while True:
                sim_elapsed = (next_at - loop_start) * speed / 60  # simulated minutes
                if sim_elapsed >= duration_min:
                    break
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

                sim_minute = start_minute + sim_elapsed
                event = make_event(rng, campaign_id, sim_origin + timedelta(minutes=sim_minute))
                task = asyncio.create_task(send(event, sim_minute))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                counts["sent"] += 1

                per_real_second = rate_per_min(sim_minute) * speed / 60
                next_at += rng.expovariate(per_real_second)

                now = time.monotonic()
                if now - last_progress >= PROGRESS_EVERY_S:
                    last_progress = now
                    print(
                        f"[{now - loop_start:6.0f}s] sim {_hhmm(sim_minute)} "
                        f"rate {rate_per_min(sim_minute):4.1f}/min  sent {counts['sent']:>6}  "
                        f"in flight {len(tasks):>4}  http errors {counts['http_errors']}"
                    )

            if tasks:
                await asyncio.gather(*tasks)
            real_duration = time.monotonic() - loop_start

        after = await take_snapshot(client, url)

    meta = {
        "url": url,
        "speed": speed,
        "start": _hhmm(start_minute),
        "duration_hours": duration_hours,
        "seed": seed,
        "campaign_id": campaign_id,
        "sent": counts["sent"],
        "real_duration_s": round(real_duration, 1),
        "before": before,
        "after": after,
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    print(
        f"done: sent {counts['sent']} in {real_duration:.0f}s, "
        f"http errors {counts['http_errors']} -> {run_path}"
    )
    return run_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the Appendix C traffic profile.")
    parser.add_argument("--url", default=os.environ.get("STEADFAST_URL", "http://localhost:8080"))
    parser.add_argument("--speed", type=float, default=60.0, help="time compression factor")
    parser.add_argument("--start", default="00:00", help="simulated start time HH:MM")
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--campaign-id", default="CAMP-2026-09-A")
    # ~3x the ~20 requests in flight at peak (50/s x ~0.4s). Measured: at 500 (and 200)
    # httpx's connection pool pinned the generator at 100% CPU once a backlog formed, so
    # client latency measured the generator, not the service; at 64 it uses ~25% CPU and
    # client p99 tracks service p99 within ~50ms.
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--out-dir", type=Path, default=Path("results"))
    parser.add_argument("--sim-date", type=date.fromisoformat, default=date(2026, 9, 1))
    args = parser.parse_args()
    if args.speed <= 0 or args.duration_hours <= 0:
        parser.error("--speed and --duration-hours must be positive")

    asyncio.run(
        run_load(
            url=args.url.rstrip("/"),
            speed=args.speed,
            start_minute=parse_hhmm(args.start),
            duration_hours=args.duration_hours,
            seed=args.seed,
            campaign_id=args.campaign_id,
            out_dir=args.out_dir,
            concurrency=args.concurrency,
            sim_date=args.sim_date,
        )
    )


if __name__ == "__main__":
    main()
