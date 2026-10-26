# Each tier only accepts traffic from the tier in front of it:
# clients -> ALB:443 (or CloudFront -> ALB:80) -> orchestrator:8080 -> mock:8081
# (and model:8080 in Phase D).

resource "aws_security_group" "alb" {
  # name_prefix + create_before_destroy: a change that forces replacement attaches the new
  # group to the ALB before deleting the old one, instead of failing on a group in use.
  name_prefix = "${var.name}-alb-"
  description = "Load balancer: HTTPS from https_cidrs, HTTP from CloudFront only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-alb" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each          = toset(var.https_cidrs)
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from clients (requests also need the access key)"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# AWS-maintained list of the addresses CloudFront uses to reach origins.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  count = var.enable_cloudfront ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  count             = var.enable_cloudfront ? 1 : 0
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront edge locations"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront[0].id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_orchestrator" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to orchestrator tasks"
  referenced_security_group_id = aws_security_group.orchestrator.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_security_group" "orchestrator" {
  name        = "${var.name}-orchestrator"
  description = "Orchestrator tasks: HTTP from the ALB only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-orchestrator" }
}

resource "aws_vpc_security_group_ingress_rule" "orchestrator_from_alb" {
  security_group_id            = aws_security_group.orchestrator.id
  description                  = "From the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# Tasks need outbound HTTPS to AWS APIs (SQS, ECR, Logs via NAT; DynamoDB/S3 via endpoints)
# and to the pipeline tier. Inbound rules are what isolate the tiers.
resource "aws_vpc_security_group_egress_rule" "orchestrator_all" {
  security_group_id = aws_security_group.orchestrator.id
  description       = "AWS APIs and the pipeline tier"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "mock" {
  name        = "${var.name}-mock"
  description = "Mock pipeline: only orchestrator tasks may call it"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-mock" }
}

resource "aws_vpc_security_group_ingress_rule" "mock_from_orchestrator" {
  security_group_id            = aws_security_group.mock.id
  description                  = "From orchestrator tasks (Service Connect)"
  referenced_security_group_id = aws_security_group.orchestrator.id
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
}

resource "aws_security_group" "model" {
  name        = "${var.name}-model"
  description = "Model tasks: only orchestrator tasks (and the replay worker) may call them"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-model" }
}

resource "aws_vpc_security_group_ingress_rule" "model_from_orchestrator" {
  security_group_id            = aws_security_group.model.id
  description                  = "From orchestrator tasks (Service Connect)"
  referenced_security_group_id = aws_security_group.orchestrator.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "model_all" {
  security_group_id = aws_security_group.model.id
  description       = "S3 weights (gateway endpoint) and CloudWatch Logs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# The model instance itself: no inbound at all (no SSH); outbound for the ECS agent,
# ECR image pulls and OS packages.
resource "aws_security_group" "model_host" {
  name        = "${var.name}-model-host"
  description = "Model EC2 host: no inbound, outbound for the ECS agent and image pulls"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${var.name}-model-host" }
}

resource "aws_vpc_security_group_egress_rule" "model_host_all" {
  security_group_id = aws_security_group.model_host.id
  description       = "ECS agent, ECR, OS packages"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_egress_rule" "mock_all" {
  security_group_id = aws_security_group.mock.id
  description       = "ECR image pull and CloudWatch Logs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
