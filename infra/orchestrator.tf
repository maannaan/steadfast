# The call service (image: service/Dockerfile) behind a public ALB, 2+ Fargate tasks in
# private subnets across two AZs.

resource "aws_lb" "main" {
  name               = "${var.name}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # A call with 6 turns holds its request open ~27 s without sending bytes; 60 s leaves
  # headroom. Uvicorn's keep-alive (75 s) is deliberately longer, so the ALB, not the
  # task, closes idle connections and never reuses one the task has just dropped.
  idle_timeout               = 60
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "orchestrator" {
  name        = "${var.name}-orchestrator"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  # On scale-in or deploy, a deregistered task keeps serving its in-flight calls for up to
  # 60 s (longer than a ~27 s call) before the ALB drops it.
  deregistration_delay = 60

  # /ready = "this task can record calls" (call log reachable), deliberately not tied to
  # pipeline health: if the pipeline is down we still accept and dead-letter calls.
  health_check {
    path                = "/ready"
    matcher             = "200"
    interval            = 10
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

locals {
  use_model = var.pipeline_target == "model"

  # Against the model a turn takes seconds, not the mock's ~0.3 s: longer timeouts and
  # one turn per call. The mock keeps the multi-turn demo settings that make it scale.
  pipeline_env = local.use_model ? {
    PIPELINE_BACKEND  = "open_weight"
    PIPELINE_URL      = "http://model:8080"
    ATTEMPT_TIMEOUT_S = tostring(var.model_attempt_timeout_s)
    CALL_DEADLINE_S   = tostring(var.model_call_deadline_s)
    TURNS_PER_CALL    = tostring(var.model_turns_per_call)
    TURN_GAP_S        = "0"
    } : {
    PIPELINE_BACKEND = "mock"
    PIPELINE_URL     = "http://mock:8081"
    TURNS_PER_CALL   = tostring(var.turns_per_call)
    TURN_GAP_S       = tostring(var.turn_gap_s)
  }

  orchestrator_env = merge(local.pipeline_env, {
    CALL_LOG_BACKEND   = "dynamodb"
    DLQ_BACKEND        = "sqs"
    AWS_REGION         = var.region
    CALL_LOG_TABLE     = aws_dynamodb_table.call_log.name
    DEAD_LETTERS_TABLE = aws_dynamodb_table.dead_letters.name
    SQS_QUEUE_URL      = aws_sqs_queue.dlq.url
    SHUTDOWN_DRAIN_S   = tostring(var.shutdown_drain_s)
    LOG_LEVEL          = "INFO"
    # Metrics leave the task as EMF log lines, which CloudWatch turns into metrics: the
    # task role needs no cloudwatch:PutMetricData (an action that only takes Resource "*").
    METRICS_BACKEND = "emf"
    SERVICE_NAME    = local.orchestrator_service_name
  })
  orchestrator_service_name = "${var.name}-orchestrator"
  replay_service_name       = "${var.name}-replay"
}

resource "aws_ecs_task_definition" "orchestrator" {
  family                   = "${var.name}-orchestrator"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.orchestrator_cpu
  memory                   = var.orchestrator_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.orchestrator_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "orchestrator"
    image     = "${aws_ecr_repository.this["orchestrator"].repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{ name = "http", containerPort = 8080, protocol = "tcp" }]
    environment  = [for k, v in local.orchestrator_env : { name = k, value = v }]

    # SIGTERM -> uvicorn finishes open requests -> lifespan drains in-flight calls for up
    # to SHUTDOWN_DRAIN_S (60) -> exit. ECS only SIGKILLs after 90 s.
    stopTimeout = 90

    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
      interval    = 10
      timeout     = 3
      retries     = 3
      startPeriod = 10
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.orchestrator.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "orchestrator"
      }
    }
  }])
}

resource "aws_ecs_service" "orchestrator" {
  name            = "${var.name}-orchestrator"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.orchestrator.arn
  desired_count   = var.orchestrator_desired_count
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  # 150% caps a deploy at 3 tasks when running 2, inside the 8 vCPU Fargate quota.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 150

  # A bad image never replaces the running one: ECS rolls back automatically.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  health_check_grace_period_seconds = 30

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.orchestrator.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.orchestrator.arn
    container_name   = "orchestrator"
    container_port   = 8080
  }

  # Client-only Service Connect: resolves http://mock:8081.
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn
  }

  # After creation the autoscaler owns the task count; an apply must not reset it mid-peak.
  lifecycle {
    ignore_changes = [desired_count]
  }

  # Service Connect clients learn endpoints when their tasks start, so the mock and model
  # services must be registered first or the first tasks cannot resolve them.
  depends_on = [
    aws_lb_listener_rule.with_key,
    aws_ecs_service.mock,
    aws_ecs_service.model,
    aws_iam_role_policy.orchestrator_task,
  ]
}
