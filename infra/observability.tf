resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/ecs/${var.name}/orchestrator"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "mock" {
  name              = "/ecs/${var.name}/mock"
  retention_in_days = var.log_retention_days
}

locals {
  # Log groups ECS tasks write to; the execution role may write to these and no others.
  task_log_groups = [
    aws_cloudwatch_log_group.orchestrator,
    aws_cloudwatch_log_group.mock,
    aws_cloudwatch_log_group.replay,
    aws_cloudwatch_log_group.model,
  ]
}
