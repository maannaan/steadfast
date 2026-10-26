"""Export the CloudWatch metrics of a test window to results/aws-<label>-<time>.json.

Why: the dashboard disappears with the stack at the end of every session; this keeps the
numbers behind the screenshots (requests, latency, tasks, in-flight, retries, DLQ) in the
repo. Called by scripts/aws_results.sh, which passes the Terraform outputs.
"""

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

NS = "CollectionsChallenge"


def metric(qid: str, ns: str, name: str, stat: str, dims: dict[str, str]) -> dict:
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": ns,
                "MetricName": name,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
            },
            "Period": 60,
            "Stat": stat,
        },
    }


def queries(a: argparse.Namespace) -> list[dict]:
    alb = {"LoadBalancer": a.lb}
    ecs = {"ClusterName": a.cluster, "ServiceName": a.orchestrator}
    orch = {"ServiceName": a.orchestrator}
    sqs = {"QueueName": a.queue}
    per_task = (
        f'SEARCH(\'{{{NS},ServiceName,TaskId}} MetricName="InFlightCalls" '
        f"ServiceName=\"{a.orchestrator}\"', 'Average', 60)"
    )
    return [
        metric("requests", "AWS/ApplicationELB", "RequestCount", "Sum", alb),
        metric("target_5xx", "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "Sum", alb),
        metric("elb_5xx", "AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", "Sum", alb),
        metric("latency_p50_s", "AWS/ApplicationELB", "TargetResponseTime", "p50", alb),
        metric("latency_p99_s", "AWS/ApplicationELB", "TargetResponseTime", "p99", alb),
        metric("running_tasks", "ECS/ContainerInsights", "RunningTaskCount", "Average", ecs),
        metric("desired_tasks", "ECS/ContainerInsights", "DesiredTaskCount", "Average", ecs),
        # 1-minute metrics, one sample per task every 10 s: fleet total = sum of task averages.
        {"Id": "inflight_total", "Expression": f"SUM({per_task})", "Period": 60},
        metric("inflight_avg_per_task", NS, "InFlightCalls", "Average", orch),
        metric("calls", NS, "Calls", "Sum", orch),
        metric("retries", NS, "Retries", "Sum", orch),
        metric("downstream_errors", NS, "DownstreamErrors", "Sum", orch),
        metric("dlq_enqueued", NS, "DLQEnqueued", "Sum", orch),
        metric("replayed_calls", NS, "Calls", "Sum", {"ServiceName": a.replay}),
        metric("queue_waiting", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", sqs),
        metric(
            "queue_in_flight", "AWS/SQS", "ApproximateNumberOfMessagesNotVisible", "Maximum", sqs
        ),
    ]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("--lb", "--cluster", "--orchestrator", "--replay", "--queue", "--region"):
        p.add_argument(flag, required=True)
    p.add_argument("--label", default="run")
    p.add_argument("--minutes", type=int, default=60)
    a = p.parse_args()

    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=a.minutes)
    cw = boto3.client("cloudwatch", region_name=a.region)
    series: dict[str, dict[str, float]] = {}
    for page in cw.get_paginator("get_metric_data").paginate(
        MetricDataQueries=queries(a), StartTime=start, EndTime=end, ScanBy="TimestampAscending"
    ):
        for r in page["MetricDataResults"]:
            points = dict(zip((t.isoformat() for t in r["Timestamps"]), r["Values"], strict=True))
            series.setdefault(r["Id"], {}).update(points)

    out = Path("results") / f"aws-{a.label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(exist_ok=True)
    body = {
        "label": a.label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "period_s": 60,
        "profile": os.environ.get("AWS_PROFILE"),
        "series": series,
    }
    out.write_text(json.dumps(body, indent=1, sort_keys=True))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
