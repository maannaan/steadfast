"""Export what the AWS sessions actually cost, from Cost Explorer, to results/costs-<date>.json.

    .venv/bin/python scripts/aws_costs.py --start 2026-10-27 [--end 2026-10-29] [--label demo]

Two views, because the account also runs things that are not Steadfast:
- tagged: costs carrying Project=steadfast. Exact, but only once Project is an active cost
  allocation tag (Terraform activate_cost_allocation_tag = true) and only from then on.
- estimate: everything in the stack's region, minus usage that is known not to be ours:
  the unrelated always-on t3.small (instance hours, its EBS volume) and its Elastic IP
  (24 public-IPv4 hours a day). Usable for sessions before the tag was active.

Only usage records are counted (RECORD_TYPE = Usage), so credits do not hide the cost.
Each run makes 2 paginated Cost Explorer requests ($0.01 each). Cost Explorer data lags
usage by up to 24 h, so run this the day after a session for complete numbers.
"""

import argparse
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3

DEFAULT_EXCLUDE = ["BoxUsage:t3.small", "EBS:VolumeUsage"]
IPV4_USAGE = "PublicIPv4:InUseAddress"


def usage_filter(*extra: dict[str, Any]) -> dict[str, Any]:
    parts = [{"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}, *extra]
    return {"And": parts} if len(parts) > 1 else parts[0]


def fetch(ce: Any, start: str, end: str, flt: dict[str, Any], group_by: list[str]) -> list[dict]:
    """Daily rows: {"day", "keys", "cost", "quantity"}."""
    rows: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "Filter": flt,
            "GroupBy": [{"Type": "DIMENSION", "Key": k} for k in group_by],
        }
        if token:
            kwargs["NextPageToken"] = token
        page = ce.get_cost_and_usage(**kwargs)
        for period in page["ResultsByTime"]:
            for g in period["Groups"]:
                rows.append(
                    {
                        "day": period["TimePeriod"]["Start"],
                        "keys": g["Keys"],
                        "cost": float(g["Metrics"]["UnblendedCost"]["Amount"]),
                        "quantity": float(g["Metrics"]["UsageQuantity"]["Amount"]),
                    }
                )
        token = page.get("NextPageToken")
        if not token:
            return rows


def summarise(rows: list[dict], key_index: int) -> dict[str, Any]:
    by_key: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    for r in rows:
        by_key[r["keys"][key_index]] += r["cost"]
        by_day[r["day"]] += r["cost"]
    return {
        "total_usd": round(sum(by_day.values()), 4),
        "by_day": {d: round(v, 4) for d, v in sorted(by_day.items()) if v >= 0.00005},
        "by_" + ("service" if key_index == 0 else "usage_type"): {
            k: round(v, 4) for k, v in sorted(by_key.items(), key=lambda kv: -kv[1]) if v >= 0.00005
        },
    }


def estimate(rows: list[dict], exclude: list[str], ipv4_hours_per_day: float) -> list[dict]:
    """Drop excluded usage types; take the other workload's IPv4 hours out of each day."""
    kept: list[dict] = []
    for r in rows:
        usage_type = r["keys"][1]
        if any(x in usage_type for x in exclude):
            continue
        if IPV4_USAGE in usage_type and r["quantity"] > 0:
            unit = r["cost"] / r["quantity"]
            ours = max(r["quantity"] - ipv4_hours_per_day, 0.0)
            r = {**r, "quantity": ours, "cost": ours * unit}
        kept.append(r)
    return kept


def redact(text: str, account: str) -> str:
    return text.replace(account, "<account>") if account else re.sub(r"\d{12}", "<account>", text)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    p.add_argument("--end", help="day after the last day (default: tomorrow)")
    p.add_argument("--label", default="session")
    p.add_argument("--region", default="ap-south-1", help="the stack's region")
    p.add_argument("--project", default="steadfast", help="value of the Project tag")
    p.add_argument(
        "--exclude",
        action="append",
        help=f"usage type substring to drop (repeatable; default {DEFAULT_EXCLUDE})",
    )
    p.add_argument(
        "--ipv4-hours-per-day",
        type=float,
        default=24.0,
        help="public IPv4 hours/day used by other workloads (default 24: one EIP)",
    )
    a = p.parse_args()

    start = date.fromisoformat(a.start)
    end = date.fromisoformat(a.end) if a.end else datetime.now(UTC).date() + timedelta(days=1)
    span = (start.isoformat(), end.isoformat())
    exclude = a.exclude or DEFAULT_EXCLUDE

    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    ce = session.client("ce", region_name="us-east-1")  # Cost Explorer's only endpoint

    tag = {"Tags": {"Key": "Project", "Values": [a.project]}}
    region = {"Dimensions": {"Key": "REGION", "Values": [a.region]}}
    tagged = fetch(ce, *span, usage_filter(tag), ["SERVICE"])
    regional = fetch(ce, *span, usage_filter(region), ["SERVICE", "USAGE_TYPE"])
    kept = estimate(regional, exclude, a.ipv4_hours_per_day)

    body = {
        "label": a.label,
        "start": span[0],
        "end_exclusive": span[1],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tagged": {"filter": f"Project={a.project}", **summarise(tagged, 0)},
        "estimate": {
            "region": a.region,
            "excluded_usage_types": exclude,
            "ipv4_hours_per_day_subtracted": a.ipv4_hours_per_day,
            **summarise(kept, 0),
            **{k: v for k, v in summarise(kept, 1).items() if k == "by_usage_type"},
        },
    }
    out = Path("results") / f"costs-{a.label}-{span[0]}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(redact(json.dumps(body, indent=1), account) + "\n")
    print(
        f"tagged Project={a.project}: ${body['tagged']['total_usd']:.2f}   "
        f"estimate ({a.region}, exclusions applied): ${body['estimate']['total_usd']:.2f}"
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
