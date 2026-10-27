# Three alarms, each tied to a promise the system makes:
# - latency: each conversational turn is answered in normal time (p99 under 3x baseline);
# - DLQ depth: nothing waits in the dead-letter queue for long without someone replaying it;
# - 5xx rate: the service accepts calls (a dead-lettered call is a 200, so 5xx means a
#   call we could not even record, the one outcome that breaks "zero unaccounted").

# Not KMS-encrypted on purpose: CloudWatch alarms cannot publish to a topic encrypted with
# the AWS-managed SNS key, and alarm messages carry no borrower data.
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

locals {
  # Whether an address was given is not sensitive, only the address is; count and
  # for_each refuse sensitive values.
  has_alert_email = nonsensitive(var.alert_email != "")
}

resource "aws_sns_topic_subscription" "email" {
  count     = local.has_alert_email ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  baseline_turn_p99_ms = var.baseline_turn_p99_ms[var.pipeline_target]
}

# Per turn, not per call: a 6-turn call is ~27 s by design, so a per-call alarm at 3x would
# sit above the ALB's 60 s idle timeout and could never fire. A turn includes its retries
# and backoff (what a borrower waits through) but not the gap between turns.
resource "aws_cloudwatch_metric_alarm" "latency_p99" {
  alarm_name          = "${var.name}-latency-p99"
  alarm_description   = "Per-turn p99 above 3x the ${var.pipeline_target} baseline (${local.baseline_turn_p99_ms} ms) for 3 of 5 minutes."
  namespace           = "CollectionsChallenge"
  metric_name         = "TurnLatencyMs"
  dimensions          = { ServiceName = local.orchestrator_service_name }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3 * local.baseline_turn_p99_ms
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.name}-dlq-not-empty"
  alarm_description   = "Dead-lettered calls have been waiting for 5 minutes: fix the cause, then run scripts/replay_aws.sh."
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  # Waiting + in-progress messages: a message the worker is replaying is still unresolved.
  metric_query {
    id          = "depth"
    expression  = "visible + inflight"
    label       = "DLQ depth"
    return_data = true
  }
  metric_query {
    id = "visible"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.dlq.name }
      stat        = "Maximum"
      period      = 60
    }
  }
  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.dlq.name }
      stat        = "Maximum"
      period      = 60
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "error_rate_5xx" {
  alarm_name          = "${var.name}-5xx-rate"
  alarm_description   = "More than 2% of requests got a 5xx (target or ALB) in 2 of 3 minutes."
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  # FILL(...,0): the ALB publishes no 5xx datapoint when there were none.
  metric_query {
    id          = "rate"
    expression  = "100 * (FILL(target5xx, 0) + FILL(elb5xx, 0)) / requests"
    label       = "5xx %"
    return_data = true
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      stat        = "Sum"
      period      = 60
    }
  }
  metric_query {
    id = "target5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_Target_5XX_Count"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      stat        = "Sum"
      period      = 60
    }
  }
  metric_query {
    id = "elb5xx"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "HTTPCode_ELB_5XX_Count"
      dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }
      stat        = "Sum"
      period      = 60
    }
  }
}
