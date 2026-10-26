# Builds the model image inside AWS on a short-lived EC2 instance instead of on the
# operator's laptop.
#
# Why not locally: the model image is ~1 GB of Python wheels and ML libraries. Built on a
# laptop it is downloaded through Docker Desktop's network, emulated (Apple Silicon ->
# amd64) and pushed back up over a home uplink; on the author's machine Docker downloads
# ran at ~30 kB/s, i.e. hours. In the VPC the build is native x86_64 next to ECR.
#
# Why not CodeBuild: it was the first choice, but new accounts start with a CodeBuild
# concurrency quota of 0 (StartBuild fails with AccountLimitExceededException) and raising
# it is a support request. A one-shot instance uses the standard EC2 quota instead.
#
# How: scripts/build_model.sh uploads model/ as a zip to s3://<models bucket>/build/ and
# launches one instance from this template with a user-data script that builds, pushes and
# powers off. Shutdown behaviour is "terminate", so a finished (or failed) build leaves
# nothing running; the script also schedules a 60-minute power-off as a dead-man's switch.
# Cost: a c7i.large for ~10 minutes, about one cent.

data "aws_ssm_parameter" "builder_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_launch_template" "builder" {
  name_prefix                          = "${var.name}-builder-"
  image_id                             = data.aws_ssm_parameter.builder_ami.value
  instance_type                        = var.builder_instance_type
  instance_initiated_shutdown_behavior = "terminate"

  iam_instance_profile {
    arn = aws_iam_instance_profile.builder.arn
  }

  # Private subnet: no public IP, outbound through the NAT (pip, dnf) and the S3 endpoint.
  network_interfaces {
    subnet_id                   = aws_subnet.private[0].id
    security_groups             = [aws_security_group.builder.id]
    associate_public_ip_address = false
    delete_on_termination       = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Room for the base image, the CUDA base for the GPU tier and Docker's build cache.
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 40
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name}-builder", Project = var.name }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "${var.name}-builder", Project = var.name }
  }
}

resource "aws_security_group" "builder" {
  name        = "${var.name}-builder"
  description = "Model image builder: no inbound, outbound for packages, S3 and ECR"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "builder_all" {
  security_group_id = aws_security_group.builder.id
  description       = "Package indexes, S3 and ECR"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------- builder role ----------

resource "aws_iam_role" "builder" {
  name               = "${var.name}-model-builder"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "builder" {
  # Same reason as the execution role: GetAuthorizationToken only accepts Resource "*" and
  # has no condition keys. Which repository can be written is limited by EcrPushModel.
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "EcrPushModel"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.this["model"].arn]
  }

  # Only the source zips under build/; the weights under models/ are not readable.
  statement {
    sid       = "ReadSource"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.models.arn}/build/*"]
  }

  # The build log, read by scripts/build_model.sh: the instance is gone by then, and
  # its console output does not survive termination.
  statement {
    sid       = "WriteBuildLog"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.models.arn}/build/logs/*"]
  }
}

resource "aws_iam_role_policy" "builder" {
  name   = "push-model-image"
  role   = aws_iam_role.builder.id
  policy = data.aws_iam_policy_document.builder.json
}

resource "aws_iam_instance_profile" "builder" {
  name = "${var.name}-model-builder"
  role = aws_iam_role.builder.name
}
