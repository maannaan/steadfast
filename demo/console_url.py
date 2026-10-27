"""A short-lived, read-only AWS console sign-in URL for screen recording.

Why federation: the recording browser needs a console session, but typing a password
into it (or storing one) is not an option. sts:GetFederationToken turns the operator's
IAM user credentials into temporary credentials limited by the session policy below, and
the AWS sign-in endpoint exchanges those for a console URL. The URL is a bearer token: it
is returned to the caller in memory and never printed or written to disk.

The session policy is an upper bound: the session can do only what both the IAM user and
this policy allow, i.e. read dashboards, alarms, metrics and service state. It is a
temporary session policy, not deployed IAM; Resource "*" keeps it valid for whatever
names a session's stack happens to have, and every action in it is a read.
"""

import json
import urllib.parse
import urllib.request

import boto3

SIGNIN = "https://signin.aws.amazon.com/federation"
ISSUER = "steadfast-demo"

READ_ONLY_ACTIONS = [
    "cloudwatch:DescribeAlarmHistory",
    "cloudwatch:DescribeAlarms",
    "cloudwatch:DescribeAlarmsForMetric",
    "cloudwatch:GetDashboard",
    "cloudwatch:GetMetricData",
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:GetMetricWidgetImage",
    "cloudwatch:ListDashboards",
    "cloudwatch:ListMetrics",
    "ecs:DescribeClusters",
    "ecs:DescribeServiceDeployments",
    "ecs:DescribeServiceRevisions",
    "ecs:DescribeServices",
    "ecs:DescribeTaskDefinition",
    "ecs:DescribeTasks",
    "ecs:ListClusters",
    "ecs:ListServiceDeployments",
    "ecs:ListServices",
    "ecs:ListTasks",
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:FilterLogEvents",
    "logs:GetLogEvents",
    "logs:GetQueryResults",
    "logs:StartQuery",
    "sqs:GetQueueAttributes",
    "sqs:ListQueues",
    "application-autoscaling:DescribeScalableTargets",
    "application-autoscaling:DescribeScalingActivities",
    "application-autoscaling:DescribeScalingPolicies",
]


def session_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": READ_ONLY_ACTIONS, "Resource": "*"}],
        }
    )


def signin_url(destination: str, duration_s: int = 3600) -> str:
    """Console URL that lands on `destination`, valid for `duration_s` seconds."""
    creds = boto3.client("sts").get_federation_token(
        Name=ISSUER, Policy=session_policy(), DurationSeconds=duration_s
    )["Credentials"]
    session = json.dumps(
        {
            "sessionId": creds["AccessKeyId"],
            "sessionKey": creds["SecretAccessKey"],
            "sessionToken": creds["SessionToken"],
        }
    )
    query = urllib.parse.urlencode(
        {"Action": "getSigninToken", "SessionDuration": duration_s, "Session": session}
    )
    with urllib.request.urlopen(f"{SIGNIN}?{query}", timeout=30) as resp:  # noqa: S310
        token = json.load(resp)["SigninToken"]
    login = urllib.parse.urlencode(
        {"Action": "login", "Issuer": ISSUER, "Destination": destination, "SigninToken": token}
    )
    return f"{SIGNIN}?{login}"


def dashboard_url(region: str, name: str, minutes: int = 30) -> str:
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#dashboards/dashboard/{name}?start=PT{minutes}M"
    )
