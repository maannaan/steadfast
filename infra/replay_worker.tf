# The DLQ replay worker: same image and task role as the orchestrator, different command
# (python -m service.replay_worker). It consumes the SQS queue and replays each event
# through the same CallExecutor, so a replay obeys the same retry, breaker and DLQ rules.
#
# Why a service with desired_count 0 and not `aws ecs run-task`: the worker must reach the
# pipeline at http://mock:8081 (later the model tier), and ECS Service Connect only wires up
# tasks that belong to a service. scripts/replay_aws.sh scales it to 1, waits for the queue
# to empty, and scales it back to 0, so it costs nothing while idle.

resource "aws_cloudwatch_log_group" "replay" {
  name              = "/ecs/${var.name}/replay"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "replay" {
  family                   = "${var.name}-replay"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.orchestrator_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "replay"
    image     = "${aws_ecr_repository.this["orchestrator"].repository_url}:${var.image_tag}"
    essential = true
    command   = ["python", "-m", "service.replay_worker"]

    environment = [
      for k, v in merge(local.orchestrator_env, {
        SERVICE_NAME       = local.replay_service_name
        REPLAY_RATE_PER_S  = tostring(var.replay_rate_per_s)
        REPLAY_CONCURRENCY = tostring(var.replay_concurrency)
      }) : { name = k, value = v }
    ]

    # SIGTERM stops the receive loop after the current batch; messages it did not finish
    # are released or reappear after the queue's visibility timeout, so none are lost.
    stopTimeout = 90

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.replay.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "replay"
      }
    }
  }])
}

resource "aws_ecs_service" "replay" {
  name            = local.replay_service_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.replay.arn
  desired_count   = 0
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  network_configuration {
    subnets = aws_subnet.private[*].id
    # The orchestrator's group: the mock only admits traffic from it, and the worker is
    # the orchestrator's code in another role. Nothing connects to the worker itself.
    security_groups  = [aws_security_group.orchestrator.id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn
  }

  # scripts/replay_aws.sh owns the task count; an apply must not reset a running drain.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_ecs_service.mock, aws_iam_role_policy.orchestrator_task]
}
