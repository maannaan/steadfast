# Least privilege, no wildcard actions. Two kinds of role:
# - execution role: used by the ECS agent to pull images and ship logs (never by our code);
# - task role: what the orchestrator's own code may do (DynamoDB items, one SQS queue).
# The mock has no task role at all: it calls no AWS APIs.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy guard: only ECS acting for this account may assume the role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# ---------- execution role ----------

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  # ecr:GetAuthorizationToken does not support resource-level permissions: AWS only
  # accepts Resource "*" for it, and it has no condition keys to narrow it. It only
  # returns a registry login token; what can be pulled is limited by the next statement.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPullOurRepos"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for r in aws_ecr_repository.this : r.arn]
  }

  statement {
    sid       = "WriteOurLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for g in local.task_log_groups : "${g.arn}:log-stream:*"]
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "pull-images-and-write-logs"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---------- orchestrator task role ----------

resource "aws_iam_role" "orchestrator_task" {
  name               = "${var.name}-orchestrator-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "orchestrator_task" {
  # Item-level access only: no CreateTable/DeleteTable/DeleteItem. Scan backs the operator
  # endpoints (/stats, /dlq listing), never the per-call path.
  statement {
    sid = "CallLogAndDlqIndex"
    actions = [
      "dynamodb:BatchGetItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.call_log.arn, aws_dynamodb_table.dead_letters.arn]
  }

  # Send for the service; Receive/Delete/ChangeVisibility for the replay worker (same
  # image and role, run as its own ECS service); GetQueueAttributes for queue depth.
  statement {
    sid = "DlqQueue"
    actions = [
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
    ]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "orchestrator_task" {
  name   = "call-log-and-dlq"
  role   = aws_iam_role.orchestrator_task.id
  policy = data.aws_iam_policy_document.orchestrator_task.json
}

# ---------- ECS infrastructure role for blue/green ----------
# Assumed by ECS itself (not by a task) to move traffic between the blue and green target
# groups. Same actions as the AWS managed AmazonECSInfrastructureRolePolicyForLoadBalancers,
# but that policy allows them on every load balancer in the account (arn ...:*:*:...);
# here the mutating ones are limited to this stack's listener, rules and target groups.

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "ecs_load_balancers" {
  name               = "${var.name}-ecs-load-balancers"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "ecs_load_balancers" {
  # Elastic Load Balancing Describe* actions do not support resource-level permissions:
  # AWS only accepts Resource "*" for them, and they have no condition keys to narrow
  # them. They are read-only.
  statement {
    sid = "ReadLoadBalancerConfig"
    actions = [
      "elasticloadbalancing:DescribeListeners",
      "elasticloadbalancing:DescribeRules",
      "elasticloadbalancing:DescribeTargetGroups",
      "elasticloadbalancing:DescribeTargetHealth",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ShiftTrafficOnOurRules"
    actions = [
      "elasticloadbalancing:ModifyListener",
      "elasticloadbalancing:ModifyRule",
    ]
    resources = [
      aws_lb_listener.https.arn,
      aws_lb_listener_rule.with_key.arn,
      aws_lb_listener_rule.green_test.arn,
    ]
  }

  statement {
    sid = "RegisterOurTargets"
    actions = [
      "elasticloadbalancing:RegisterTargets",
      "elasticloadbalancing:DeregisterTargets",
    ]
    resources = [for tg in aws_lb_target_group.orchestrator : tg.arn]
  }
}

resource "aws_iam_role_policy" "ecs_load_balancers" {
  name   = "blue-green-traffic-shift"
  role   = aws_iam_role.ecs_load_balancers.id
  policy = data.aws_iam_policy_document.ecs_load_balancers.json
}

# ---------- model task role ----------

resource "aws_iam_role" "model_task" {
  name               = "${var.name}-model-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# Read-only, and only the weights: the model calls no other AWS API.
data "aws_iam_policy_document" "model_task" {
  statement {
    sid       = "ReadWeights"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.models.arn}/models/*"]
  }
  statement {
    sid       = "ListWeights"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.models.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/*"]
    }
  }
}

resource "aws_iam_role_policy" "model_task" {
  name   = "read-model-weights"
  role   = aws_iam_role.model_task.id
  policy = data.aws_iam_policy_document.model_task.json
}

# ---------- model host (EC2 instance) role ----------
# Used only by the ECS agent on the model instance to join the cluster and report task
# state. Written out instead of AmazonEC2ContainerServiceforEC2Role, which grants
# wildcard actions (ecs:Submit*) and ECR/Logs access the agent does not need here (the
# task execution role pulls the image and ships the logs).

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "model_host" {
  name               = "${var.name}-model-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

locals {
  container_instances_arn = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:container-instance/${aws_ecs_cluster.main.name}/*"
  task_sets_arn           = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-set/${aws_ecs_cluster.main.name}/*"
}

data "aws_iam_policy_document" "model_host" {
  statement {
    sid = "JoinAndReportToOurCluster"
    actions = [
      "ecs:DeregisterContainerInstance",
      "ecs:RegisterContainerInstance",
      "ecs:SubmitAttachmentStateChanges",
      "ecs:SubmitContainerStateChange",
      "ecs:SubmitTaskStateChange",
    ]
    resources = [aws_ecs_cluster.main.arn]
  }

  statement {
    sid = "ActAsOurContainerInstances"
    actions = [
      "ecs:Poll",
      "ecs:StartTelemetrySession",
      "ecs:UpdateContainerInstancesState",
    ]
    resources = [local.container_instances_arn]
  }

  # Service Connect on EC2: the agent relays the proxy's xDS config stream and authorises
  # it as ecs:Poll on the service's task set. Without it the proxy never turns healthy and
  # the model container, which waits for it, never starts.
  statement {
    sid       = "ServiceConnectConfig"
    actions   = ["ecs:Poll"]
    resources = [local.task_sets_arn]
  }

  # ecs:DiscoverPollEndpoint has no resource-level permissions or condition keys: AWS
  # only accepts Resource "*". It returns the agent's endpoint URL and nothing else.
  statement {
    sid       = "FindAgentEndpoint"
    actions   = ["ecs:DiscoverPollEndpoint"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "model_host" {
  name   = "ecs-agent"
  role   = aws_iam_role.model_host.id
  policy = data.aws_iam_policy_document.model_host.json
}

resource "aws_iam_instance_profile" "model_host" {
  name = "${var.name}-model-host"
  role = aws_iam_role.model_host.name
}
