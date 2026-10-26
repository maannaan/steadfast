# ECS lifecycle events (task started/stopped and why, service steady/unhealthy, scaling)
# go to a log group, so "why did a task stop at 08:12?" has an answer after the fact.
# ECS only keeps stopped tasks visible for about an hour.

resource "aws_cloudwatch_log_group" "ecs_events" {
  # EventBridge can only deliver to log groups whose name starts with /aws/events/.
  name              = "/aws/events/${var.name}/ecs"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_event_rule" "ecs" {
  name        = "${var.name}-ecs-events"
  description = "ECS task state changes and service actions in the ${var.name} cluster."
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change", "ECS Service Action"]
    detail        = { clusterArn = [aws_ecs_cluster.main.arn] }
  })
}

resource "aws_cloudwatch_event_target" "ecs_to_logs" {
  rule = aws_cloudwatch_event_rule.ecs.name
  arn  = aws_cloudwatch_log_group.ecs_events.arn
}

# Log groups have no per-group policy; this account-level policy grants EventBridge write
# access to the one group above, and only on behalf of the one rule above.
data "aws_iam_policy_document" "ecs_events_to_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ecs_events.arn}:*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.ecs.arn]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "ecs_events" {
  policy_name     = "${var.name}-ecs-events-to-logs"
  policy_document = data.aws_iam_policy_document.ecs_events_to_logs.json
}
