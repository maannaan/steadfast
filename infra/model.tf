# The open-weight model tier (image: model/Dockerfile): STT -> LLM -> TTS on one EC2
# instance per task, reachable only via Service Connect as http://model:8080.
#
# Why ECS on EC2 and not Fargate: Fargate has no GPUs, and the CPU tier wants a whole
# compute-optimised host. Why a capacity provider with an Auto Scaling group at min 0: the
# instance exists only while model_desired_count > 0, so the tier costs nothing at rest and
# scripts/model_on.sh / model_off.sh are the only switches.

locals {
  model_tiers = {
    cpu = {
      instance_type = "c7i.xlarge" # 4 vCPU: fits the 8 vCPU standard quota beside other instances
      ami_parameter = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
      task_cpu      = 4096
      task_memory   = 6144
      gpus          = 0
      env = {
        WHISPER_DIR          = "faster-whisper-small"
        WHISPER_DEVICE       = "cpu"
        WHISPER_COMPUTE_TYPE = "int8"
        LLM_FILE             = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
        LLM_GPU_LAYERS       = "0"
      }
    }
    gpu = {
      instance_type = "g5.xlarge" # 1x A10G 24 GB, 4 vCPU, 16 GiB
      ami_parameter = "/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id"
      task_cpu      = 4096
      task_memory   = 14336
      gpus          = 1
      env = {
        WHISPER_DIR          = "faster-whisper-large-v3-turbo"
        WHISPER_DEVICE       = "cuda"
        WHISPER_COMPUTE_TYPE = "int8_float16"
        LLM_FILE             = "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf"
        LLM_GPU_LAYERS       = "-1"
      }
    }
  }
  model_tier    = local.model_tiers[var.model_compute]
  model_weights = "models/${var.model_compute}/"
}

# ---------- weights bucket ----------

# Filled by scripts/upload_models.sh; read by the model task through the S3 gateway
# endpoint (no NAT charge). force_destroy: weights are re-uploadable, and teardown must
# leave nothing behind.
resource "aws_s3_bucket" "models" {
  bucket_prefix = "${var.name}-models-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "models" {
  bucket = aws_s3_bucket.models.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "models_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.models.arn, "${aws_s3_bucket.models.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "models" {
  bucket     = aws_s3_bucket.models.id
  policy     = data.aws_iam_policy_document.models_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.models]
}

# ---------- host instances ----------

data "aws_ssm_parameter" "model_ami" {
  name = local.model_tier.ami_parameter
}

resource "aws_launch_template" "model" {
  name_prefix            = "${var.name}-model-"
  image_id               = data.aws_ssm_parameter.model_ami.value
  instance_type          = local.model_tier.instance_type
  vpc_security_group_ids = [aws_security_group.model_host.id]

  iam_instance_profile {
    arn = aws_iam_instance_profile.model_host.arn
  }

  # IMDSv2 only, one hop: containers (awsvpc tasks) cannot reach the instance role's
  # credentials; they get their own task role from the ECS agent instead.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Room for the image (~1-5 GB) and the weights (1.6 GB CPU, 6 GB GPU).
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 60
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    cat >>/etc/ecs/ecs.config <<CONFIG
    ECS_CLUSTER=${aws_ecs_cluster.main.name}
    ECS_AWSVPC_BLOCK_IMDS=true
    ECS_IMAGE_PULL_BEHAVIOR=prefer-cached
    CONFIG
    # Weights cache, mounted into the task at /models; owned by the container's user.
    mkdir -p /opt/steadfast-models && chown 10001:10001 /opt/steadfast-models
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name}-model", Project = var.name }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${var.name}-model", Project = var.name }
  }
}

resource "aws_autoscaling_group" "model" {
  name_prefix         = "${var.name}-model-"
  vpc_zone_identifier = aws_subnet.private[*].id
  min_size            = 0
  max_size            = 1
  force_delete        = true

  launch_template {
    id      = aws_launch_template.model.id
    version = aws_launch_template.model.latest_version
  }

  # ECS (the capacity provider) decides how many instances run.
  lifecycle {
    ignore_changes = [desired_capacity]
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
  tag {
    key                 = "Project"
    value               = var.name
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "model" {
  name = "${var.name}-model"

  auto_scaling_group_provider {
    auto_scaling_group_arn = aws_autoscaling_group.model.arn
    # Off so destroy and model_off never wait on instance scale-in protection; managed
    # draining still moves tasks off an instance before it terminates.
    managed_termination_protection = "DISABLED"
    managed_draining               = "ENABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
    }
  }
}

# ---------- task ----------

resource "aws_cloudwatch_log_group" "model" {
  name              = "/ecs/${var.name}/model"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "model" {
  family                   = "${var.name}-model"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = local.model_tier.task_cpu
  memory                   = local.model_tier.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.model_task.arn

  volume {
    name      = "models"
    host_path = "/opt/steadfast-models"
  }

  container_definitions = jsonencode([merge(local.model_tier.gpus > 0 ? {
    resourceRequirements = [{ type = "GPU", value = tostring(local.model_tier.gpus) }]
    } : {}, {
    name      = "model"
    image     = "${aws_ecr_repository.this["model"].repository_url}:${var.model_image_tag}"
    essential = true

    portMappings = [{ name = "http", containerPort = 8080, protocol = "tcp" }]
    environment = [
      for k, v in merge(local.model_tier.env, {
        WEIGHTS_BUCKET  = aws_s3_bucket.models.id
        WEIGHTS_PREFIX  = local.model_weights
        AWS_REGION      = var.region
        FAULT_RATE      = tostring(var.model_fault_rate)
        QUEUE_TIMEOUT_S = tostring(var.model_queue_timeout_s)
      }) : { name = k, value = v }
    ]
    mountPoints = [{ sourceVolume = "models", containerPath = "/models" }]

    # /health is 503 until weights are fetched, models loaded and one warm-up turn done.
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 300
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.model.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "model"
      }
    }
  })])
}

resource "aws_ecs_service" "model" {
  name            = "${var.name}-model"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.model.arn
  desired_count   = var.model_desired_count
  propagate_tags  = "SERVICE"

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.model.name
    weight            = 1
  }

  # One task per instance and one instance at most: a new version replaces the old one
  # (brief gap, the orchestrator dead-letters and replays) rather than needing a second host.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.model.id]
    assign_public_ip = false
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.main.arn

    service {
      port_name      = "http"
      discovery_name = "model"

      client_alias {
        port     = 8080
        dns_name = "model"
      }
    }

    # The Service Connect proxy gates the model container's start (it must be healthy
    # first); without its logs a stuck proxy is invisible.
    log_configuration {
      log_driver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.model.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "service-connect"
      }
    }
  }

  depends_on = [
    aws_ecs_cluster_capacity_providers.main,
    aws_iam_role_policy.execution,
    aws_iam_role_policy.model_task,
  ]
}
