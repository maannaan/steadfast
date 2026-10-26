resource "aws_ecs_cluster" "main" {
  name = var.name

  # Per-task CPU/memory and running-task counts in CloudWatch, for the dashboard.
  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  service_connect_defaults {
    namespace = aws_service_discovery_http_namespace.main.arn
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name
  # FARGATE for the orchestrator, mock and replay worker; the EC2 provider for the model.
  capacity_providers = ["FARGATE", aws_ecs_capacity_provider.model.name]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}
