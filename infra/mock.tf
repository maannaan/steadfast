# The flaky mock pipeline (image: mock/Dockerfile), reachable only via Service Connect
# as http://mock:8081. One task: it is a test double, not something to make highly available.

resource "aws_ecs_task_definition" "mock" {
  family                   = "${var.name}-mock"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.mock_cpu
  memory                   = var.mock_memory
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "mock"
    image     = "${aws_ecr_repository.this["mock"].repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{ name = "http", containerPort = 8081, protocol = "tcp" }]
    environment = [
      { name = "FAILURE_RATE", value = tostring(var.mock_failure_rate) },
      { name = "FAILURE_MODE", value = var.mock_failure_mode },
    ]

    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=2)"]
      interval    = 10
      timeout     = 3
      retries     = 3
      startPeriod = 10
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.mock.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "mock"
      }
    }
  }])
}

resource "aws_ecs_service" "mock" {
  name            = "${var.name}-mock"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mock.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  propagate_tags  = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.mock.id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn

    service {
      port_name      = "http"
      discovery_name = "mock"

      client_alias {
        port     = 8081
        dns_name = "mock"
      }
    }
  }

  depends_on = [aws_iam_role_policy.execution]
}
