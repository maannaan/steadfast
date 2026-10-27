# One dashboard that answers, in order: is traffic flowing, is it healthy, how loaded is
# each task, did we scale, is the pipeline failing, and is anything waiting in the DLQ.
# Application metrics come from EMF log lines in namespace CollectionsChallenge.

locals {
  metric_ns = "CollectionsChallenge"
  alb_dim   = ["LoadBalancer", aws_lb.main.arn_suffix]
  orch_dim  = ["ServiceName", local.orchestrator_service_name]
  replay_d  = ["ServiceName", local.replay_service_name]
  ecs_dims  = ["ClusterName", aws_ecs_cluster.main.name, "ServiceName", aws_ecs_service.orchestrator.name]
  sqs_dim   = ["QueueName", aws_sqs_queue.dlq.name]

  inflight_per_task = "SEARCH('{${local.metric_ns},ServiceName,TaskId} MetricName=\"InFlightCalls\" ServiceName=\"${local.orchestrator_service_name}\"', 'Average', 60)"

  widget_defaults = { region = var.region, view = "timeSeries", stacked = false, period = 60 }

  dashboard_widgets = [
    {
      title = "ALB requests and 5xx"
      metrics = [
        concat(["AWS/ApplicationELB", "RequestCount"], local.alb_dim, [{ stat = "Sum" }]),
        concat(["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count"], local.alb_dim, [{ stat = "Sum" }]),
        concat(["AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count"], local.alb_dim, [{ stat = "Sum" }]),
      ]
    },
    {
      # Per turn, with the alarm threshold drawn in. A whole call is several turns plus
      # gaps by design (~27 s on the demo settings), so its time at the ALB says little.
      title = "Turn latency (ms) vs latency alarm"
      metrics = [
        concat([local.metric_ns, "TurnLatencyMs"], local.orch_dim, [{ stat = "p50", label = "p50" }]),
        concat([local.metric_ns, "TurnLatencyMs"], local.orch_dim, [{ stat = "p99", label = "p99" }]),
      ]
      annotations = {
        horizontal = [{ label = "alarm: 3x baseline p99", value = 3 * local.baseline_turn_p99_ms }]
      }
    },
    {
      # EMF metrics are 1-minute resolution and each task publishes every 10 s, so a plain
      # Sum would count every task six times. Sum of per-task minute averages is the fleet total.
      title   = "In-flight calls: total (all tasks)"
      metrics = [[{ expression = "SUM(${local.inflight_per_task})", id = "total", label = "in flight" }]]
    },
    {
      title   = "In-flight calls per task"
      metrics = [[{ expression = local.inflight_per_task, id = "pertask" }]]
    },
    {
      title = "Orchestrator tasks: running vs desired"
      metrics = [
        concat(["ECS/ContainerInsights", "RunningTaskCount"], local.ecs_dims, [{ stat = "Average" }]),
        concat(["ECS/ContainerInsights", "DesiredTaskCount"], local.ecs_dims, [{ stat = "Average" }]),
      ]
    },
    {
      title = "Calls, retries and pipeline errors"
      metrics = [
        concat([local.metric_ns, "Calls"], local.orch_dim, [{ stat = "Sum" }]),
        concat([local.metric_ns, "Retries"], local.orch_dim, [{ stat = "Sum" }]),
        concat([local.metric_ns, "DownstreamErrors"], local.orch_dim, [{ stat = "Sum" }]),
        concat([local.metric_ns, "Calls"], local.replay_d, [{ stat = "Sum", label = "Replayed calls" }]),
      ]
    },
    {
      title = "Tasks whose circuit breaker was open"
      metrics = [[{
        expression = "SUM(SEARCH('{${local.metric_ns},ServiceName,TaskId} MetricName=\"CircuitOpen\" ServiceName=\"${local.orchestrator_service_name}\"', 'Maximum', 60))"
        id         = "open"
        label      = "tasks with breaker open"
      }]]
    },
    {
      title = "Dead-lettered calls and DLQ depth"
      metrics = [
        concat([local.metric_ns, "DLQEnqueued"], local.orch_dim, [{ stat = "Sum" }]),
        concat(["AWS/SQS", "ApproximateNumberOfMessagesVisible"], local.sqs_dim, [{ stat = "Maximum", label = "queue: waiting" }]),
        concat(["AWS/SQS", "ApproximateNumberOfMessagesNotVisible"], local.sqs_dim, [{ stat = "Maximum", label = "queue: being replayed" }]),
      ]
    },
  ]
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = concat(
      [for i, w in local.dashboard_widgets : {
        type       = "metric"
        x          = (i % 2) * 12
        y          = floor(i / 2) * 6
        width      = 12
        height     = 6
        properties = merge(local.widget_defaults, w)
      }],
      [{
        type   = "log"
        x      = 0
        y      = ceil(length(local.dashboard_widgets) / 2) * 6
        width  = 24
        height = 6
        properties = {
          title  = "Recent calls that did not complete"
          region = var.region
          view   = "table"
          query  = <<-EOT
            SOURCE '${aws_cloudwatch_log_group.orchestrator.name}' | SOURCE '${aws_cloudwatch_log_group.replay.name}'
            | fields @timestamp, event_id, outcome, retries, dlq_reason, replay, @logStream
            | filter event = "call" and outcome = "failed_downstream"
            | sort @timestamp desc
            | limit 50
          EOT
        }
      }],
    )
  })
}
