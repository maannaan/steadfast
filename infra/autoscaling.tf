# Scale the orchestrator on the load it actually carries: calls in flight per task.
#
# Why in-flight calls and not CPU or request count: a collections call is a long-held
# request that mostly waits on the pipeline (CPU stays low while a task juggles dozens of
# calls), and RequestCount says nothing about how long each one is held. In-flight per
# task is the quantity that runs out (connections, memory, breaker isolation), and by
# Little's Law it already folds in both arrival rate and call duration.
#
# The metric is the Average of InFlightCalls over all samples: every task reports every
# 10 s, so this equals total in flight / running tasks. That is what target tracking needs
# (a per-task value that falls as tasks are added), without the "Sum of six 10 s samples
# per minute" trap and without depending on Container Insights.

resource "aws_appautoscaling_target" "orchestrator" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.orchestrator.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  # Two tasks in different AZs is the floor for availability, not for load.
  min_capacity = var.orchestrator_desired_count
  # 10 x 0.5 vCPU + mock + replay worker = 6 of the account's 8 Fargate vCPU.
  max_capacity = var.orchestrator_max_count
}

resource "aws_appautoscaling_policy" "inflight" {
  name               = "${var.name}-inflight-per-task"
  service_namespace  = aws_appautoscaling_target.orchestrator.service_namespace
  resource_id        = aws_appautoscaling_target.orchestrator.resource_id
  scalable_dimension = aws_appautoscaling_target.orchestrator.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value = var.inflight_target_per_task
    # Out fast (a campaign ramp reaches peak in 15 minutes), in slowly: removing a task
    # costs a 60 s connection drain, and flapping would cost it repeatedly.
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    customized_metric_specification {
      namespace   = "CollectionsChallenge"
      metric_name = "InFlightCalls"
      statistic   = "Average"
      unit        = "Count"

      dimensions {
        name  = "ServiceName"
        value = local.orchestrator_service_name
      }
    }
  }
}

# Campaigns start on a known schedule, and a new task needs ~1 minute to start and pass
# health checks. Raising the floor 15 minutes before the window avoids that lag on the
# ramp. Off by default so the demo shows reactive scaling on its own.
resource "aws_appautoscaling_scheduled_action" "campaign_prescale" {
  for_each = var.enable_scheduled_prescale ? {
    before_morning_window = { cron = "cron(45 7 * * ? *)", min = var.prescale_min_count }
    after_morning_window  = { cron = "cron(15 10 * * ? *)", min = var.orchestrator_desired_count }
  } : {}

  name               = "${var.name}-${replace(each.key, "_", "-")}"
  service_namespace  = aws_appautoscaling_target.orchestrator.service_namespace
  resource_id        = aws_appautoscaling_target.orchestrator.resource_id
  scalable_dimension = aws_appautoscaling_target.orchestrator.scalable_dimension
  schedule           = each.value.cron
  timezone           = "Asia/Kolkata"

  scalable_target_action {
    min_capacity = each.value.min
    max_capacity = var.orchestrator_max_count
  }
}
