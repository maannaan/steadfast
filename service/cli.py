"""Operator CLI for the dead-letter queue.

Talks to the service's HTTP API (STEADFAST_URL, default http://localhost:8080) rather than
opening SQLite directly, so it works the same against a local process, a container, or a
deployed service, and replays go through the live executor and its circuit breaker.

  python -m service.cli dlq list [--status pending] [--reason circuit_open] [--limit 100]
  python -m service.cli dlq show <event_id>
  python -m service.cli dlq replay (--all | --id X [--id Y ...]) [--rate 5]
  python -m service.cli dlq stats
"""

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8080"


def _client(url: str) -> httpx.Client:
    # Replays are deliberately slow (rate-limited), so no read timeout.
    return httpx.Client(base_url=url, timeout=httpx.Timeout(10.0, read=None))


def _get(url: str, path: str, **params: Any) -> Any:
    with _client(url) as client:
        response = client.get(path, params={k: v for k, v in params.items() if v is not None})
    if response.status_code == 404:
        sys.exit(f"not found: {path}")
    response.raise_for_status()
    return response.json()


def cmd_list(args: argparse.Namespace) -> None:
    data = _get(args.url, "/dlq", status=args.status, reason=args.reason, limit=args.limit)
    print(f"{'event_id':<38} {'status':<14} {'reason':<22} {'att':>3} {'rep':>3}  last_failed_at")
    for item in data["items"]:
        print(
            f"{item['event_id']:<38} {item['status']:<14} {item['reason'][:22]:<22} "
            f"{item['attempts']:>3} {item['replay_count']:>3}  {item['last_failed_at']}"
        )
    print(f"\n{data['count']} entr{'y' if data['count'] == 1 else 'ies'}")


def cmd_show(args: argparse.Namespace) -> None:
    print(json.dumps(_get(args.url, f"/dlq/{args.event_id}"), indent=2))


def cmd_replay(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"rate_per_s": args.rate}
    if args.all:
        body["all"] = True
    else:
        body["event_ids"] = args.ids
    with _client(args.url) as client:
        response = client.post("/dlq/replay", json=body)
    response.raise_for_status()
    summary = response.json()
    print(json.dumps(summary, indent=2))
    if summary["failed"] or summary["aborted"]:
        sys.exit(1)


def cmd_stats(args: argparse.Namespace) -> None:
    stats = _get(args.url, "/stats")
    for key, value in stats.items():
        print(f"{key:<24} {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m service.cli")
    parser.add_argument("--url", default=os.environ.get("STEADFAST_URL", DEFAULT_URL))
    top = parser.add_subparsers(dest="group", required=True)
    dlq = top.add_parser("dlq", help="inspect and replay the dead-letter queue")
    sub = dlq.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list DLQ entries")
    p.add_argument("--status", choices=["pending", "replayed", "replay_failed"])
    p.add_argument("--reason", help="prefix match, e.g. circuit_open or non_retryable")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one DLQ entry with its full payload")
    p.add_argument("event_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("replay", help="resubmit DLQ entries through the executor")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="every entry not yet replayed")
    group.add_argument("--id", dest="ids", action="append", help="event_id (repeatable)")
    p.add_argument("--rate", type=float, default=5.0, help="replays per second")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("stats", help="call and DLQ totals")
    p.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except httpx.HTTPError as exc:
        sys.exit(f"request to {args.url} failed: {exc}")


if __name__ == "__main__":
    main()
