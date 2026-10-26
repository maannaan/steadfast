"""Prove that `terraform destroy` left nothing billable behind.

Why a separate check: `terraform state list` being empty only proves Terraform *thinks* it
deleted everything. This asks AWS directly. Every query is scoped by the Project tag (or a
name that only Steadfast uses), so unrelated resources in the same account and region are
never reported or touched; the script is read-only.

The tagging API keeps listing deleted resources for a while (hours for some types) and
INACTIVE task-definition revisions forever. So each ARN it returns is confirmed against
its own service (is_dead); confirmed-gone ones are listed as ignored, not as failures.
Anything still alive is retried until it disappears or the deadline passes, because
deletions (NAT gateways, ENIs) are eventually consistent.

Usage: teardown_verify.py --region ap-south-1 --project steadfast --out results/x.txt
Exit code 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

GLOBAL_REGION = "us-east-1"  # CloudFront, its ACM certificate and WAF live here.
ACCOUNT_IN_ARN = re.compile(r"(:)\d{12}(:)")
Check = tuple[str, Callable[[], tuple[list[str], list[str]]]]


def redact(text: str, account: str) -> str:
    """Remove the account ID wherever it appears: the output is committed under results/."""
    return ACCOUNT_IN_ARN.sub(r"\1<account>\2", text.replace(account, "<account>"))


def tagged_arns(session: boto3.Session, region: str, project: str) -> list[str]:
    client = session.client("resourcegroupstaggingapi", region_name=region)
    arns: list[str] = []
    for page in client.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": "Project", "Values": [project]}]
    ):
        arns += [r["ResourceARN"] for r in page["ResourceTagMappingList"]]
    return arns


# EC2 resource type in the ARN -> (describe call, ID parameter, result key, state field).
# A NotFound error or a final state means the resource is gone.
EC2_LOOKUPS = {
    "instance": ("describe_instances", "InstanceIds", None, None),
    "natgateway": ("describe_nat_gateways", "NatGatewayIds", "NatGateways", "State"),
    "vpc-endpoint": ("describe_vpc_endpoints", "VpcEndpointIds", "VpcEndpoints", "State"),
    "volume": ("describe_volumes", "VolumeIds", "Volumes", None),
    "vpc": ("describe_vpcs", "VpcIds", "Vpcs", None),
    "subnet": ("describe_subnets", "SubnetIds", "Subnets", None),
    "security-group": ("describe_security_groups", "GroupIds", "SecurityGroups", None),
    "security-group-rule": (
        "describe_security_group_rules",
        "SecurityGroupRuleIds",
        "SecurityGroupRules",
        None,
    ),
    "route-table": ("describe_route_tables", "RouteTableIds", "RouteTables", None),
    "internet-gateway": (
        "describe_internet_gateways",
        "InternetGatewayIds",
        "InternetGateways",
        None,
    ),
    "network-interface": (
        "describe_network_interfaces",
        "NetworkInterfaceIds",
        "NetworkInterfaces",
        None,
    ),
    "elastic-ip": ("describe_addresses", "AllocationIds", "Addresses", None),
}
FINAL_STATES = {"deleted", "terminated", "failed", "rejected", "expired"}


def is_dead(session: boto3.Session, region: str, arn: str) -> bool:
    """True if the resource behind a tagging-API ARN no longer exists or cannot be billed.

    The tagging API is an index that lags deletions (often by hours), so every ARN it
    lists is confirmed against its own service. Unknown types count as alive: the check
    must never pass by not understanding something.
    """
    service, resource = arn.split(":")[2], arn.split(":", 5)[5]
    kind, _, rest = resource.partition("/")
    try:
        if service == "ecs":
            return _ecs_dead(session.client("ecs", region_name=region), arn, kind, rest)
        if service == "ec2" and kind in EC2_LOOKUPS:
            return _ec2_dead(session.client("ec2", region_name=region), kind, rest)
    except ClientError as exc:
        return "NotFound" in exc.response["Error"]["Code"]
    return False


def _ecs_dead(ecs, arn: str, kind: str, rest: str) -> bool:  # type: ignore[no-untyped-def]
    if kind == "task-definition":
        return (
            ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]["status"] != "ACTIVE"
        )
    if kind == "cluster":
        found = ecs.describe_clusters(clusters=[arn])["clusters"]
        return all(c["status"] != "ACTIVE" for c in found)
    if kind == "service":
        cluster = rest.split("/")[0]
        found = ecs.describe_services(cluster=cluster, services=[arn])["services"]
        return all(s["status"] != "ACTIVE" for s in found)
    if kind == "task":
        cluster = rest.split("/")[0]
        found = ecs.describe_tasks(cluster=cluster, tasks=[arn])["tasks"]
        return all(t["lastStatus"] == "STOPPED" for t in found)
    return False


def _ec2_dead(ec2, kind: str, resource_id: str) -> bool:  # type: ignore[no-untyped-def]
    method, param, key, state_field = EC2_LOOKUPS[kind]
    out = getattr(ec2, method)(**{param: [resource_id]})
    if kind == "instance":
        found = [i for r in out["Reservations"] for i in r["Instances"]]
        return all(i["State"]["Name"] == "terminated" for i in found)
    items = out[key]
    if not items:
        return True
    if state_field:
        return all(str(i[state_field]).lower() in FINAL_STATES for i in items)
    return False


def checks(session: boto3.Session, region: str, project: str) -> list[Check]:
    ec2 = session.client("ec2", region_name=region)
    tag = [{"Name": "tag:Project", "Values": [project]}]

    def tagging(r: str) -> Callable[[], tuple[list[str], list[str]]]:
        def run() -> tuple[list[str], list[str]]:
            live, ignored = [], []
            for arn in tagged_arns(session, r, project):
                (ignored if is_dead(session, r, arn) else live).append(arn)
            return live, ignored

        return run

    def instances() -> tuple[list[str], list[str]]:
        res = ec2.describe_instances(Filters=tag)["Reservations"]
        found = [(i["InstanceId"], i["State"]["Name"]) for r in res for i in r["Instances"]]
        return (
            [f"{i} ({s})" for i, s in found if s != "terminated"],
            [f"{i} (terminated)" for i, s in found if s == "terminated"],
        )

    def nat() -> tuple[list[str], list[str]]:
        gws = ec2.describe_nat_gateways(Filters=tag)["NatGateways"]
        return [f"{g['NatGatewayId']} ({g['State']})" for g in gws if g["State"] != "deleted"], []

    def eips() -> tuple[list[str], list[str]]:
        return [a["AllocationId"] for a in ec2.describe_addresses(Filters=tag)["Addresses"]], []

    def volumes() -> tuple[list[str], list[str]]:
        return [v["VolumeId"] for v in ec2.describe_volumes(Filters=tag)["Volumes"]], []

    def vpcs() -> tuple[list[str], list[str]]:
        return [v["VpcId"] for v in ec2.describe_vpcs(Filters=tag)["Vpcs"]], []

    def load_balancers() -> tuple[list[str], list[str]]:
        elb = session.client("elbv2", region_name=region)
        arns = [lb["LoadBalancerArn"] for lb in elb.describe_load_balancers()["LoadBalancers"]]
        live = []
        for i in range(0, len(arns), 20):  # describe_tags takes at most 20 ARNs.
            for d in elb.describe_tags(ResourceArns=arns[i : i + 20])["TagDescriptions"]:
                if {"Key": "Project", "Value": project} in d["Tags"]:
                    live.append(d["ResourceArn"])
        return live, []

    def clusters() -> tuple[list[str], list[str]]:
        ecs = session.client("ecs", region_name=region)
        arns = ecs.list_clusters()["clusterArns"]
        if not arns:
            return [], []
        described = ecs.describe_clusters(clusters=arns, include=["TAGS"])["clusters"]
        return [
            c["clusterArn"]
            for c in described
            if c["status"] == "ACTIVE"
            and (
                c["clusterName"] == project
                or {"key": "Project", "value": project} in c.get("tags", [])
            )
        ], []

    return [
        (f"tagging API Project={project} ({region})", tagging(region)),
        (f"tagging API Project={project} ({GLOBAL_REGION})", tagging(GLOBAL_REGION)),
        ("EC2 instances", instances),
        ("NAT gateways", nat),
        ("Elastic IPs", eips),
        ("EBS volumes", volumes),
        ("VPCs", vpcs),
        ("load balancers", load_balancers),
        ("ECS clusters", clusters),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", required=True)
    parser.add_argument("--project", default="steadfast")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--deadline-s", type=int, default=600)
    args = parser.parse_args()

    session = boto3.Session()
    deadline = time.monotonic() + args.deadline_s
    while True:
        results = [(name, *run()) for name, run in checks(session, args.region, args.project)]
        if all(not live for _, live, _ in results) or time.monotonic() > deadline:
            break
        print("resources still present, re-checking in 30 s...", file=sys.stderr)
        time.sleep(30)

    passed = all(not live for _, live, _ in results)
    lines = [
        f"teardown verification {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"region={args.region} project={args.project}",
        "",
    ]
    for name, live, ignored in results:
        lines.append(f"[{'FAIL' if live else 'ok'}] {name}: {len(live)} remaining")
        lines += [f"    remaining: {x}" for x in live]
        lines += [f"    ignored (not billable): {x}" for x in ignored]
    lines += ["", f"RESULT: {'PASS' if passed else 'FAIL'}"]
    account = session.client("sts").get_caller_identity()["Account"]
    text = redact("\n".join(lines) + "\n", account)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text)
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
