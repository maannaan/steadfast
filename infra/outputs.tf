output "service_url" {
  description = "Public HTTPS URL of the call service. Every request needs the x-steadfast-key header."
  value = (var.enable_cloudfront
    ? "https://${aws_cloudfront_distribution.main[0].domain_name}"
  : "https://${aws_lb.main.dns_name}")
}

output "alb_certificate_pem" {
  description = "Self-signed certificate of the ALB's HTTPS listener (public, not a secret). Trust it explicitly: curl --cacert, or STEADFAST_CA_BUNDLE for the loadgen."
  value       = tls_self_signed_cert.alb.cert_pem
}

output "access_key" {
  description = "Value for the x-steadfast-key header. Share it only with people who should test the service."
  value       = random_password.access_key.result
  sensitive   = true
}

output "region" {
  value = var.region
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "orchestrator_service" {
  value = aws_ecs_service.orchestrator.name
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "call_log_table" {
  value = aws_dynamodb_table.call_log.name
}

output "dead_letters_table" {
  value = aws_dynamodb_table.dead_letters.name
}

output "dlq_queue_url" {
  value = aws_sqs_queue.dlq.url
}

output "replay_service" {
  description = "DLQ replay worker service (desired count 0 at rest; scripts/replay_aws.sh scales it)."
  value       = aws_ecs_service.replay.name
}

output "builder_launch_template" {
  description = "Launch template of the one-shot model image builder (scripts/build_model.sh)."
  value       = aws_launch_template.builder.id
}

output "models_bucket" {
  description = "S3 bucket for model weights (scripts/upload_models.sh)."
  value       = aws_s3_bucket.models.id
}

output "model_service" {
  value = aws_ecs_service.model.name
}

output "pipeline_target" {
  value = var.pipeline_target
}

output "dlq_queue_name" {
  value = aws_sqs_queue.dlq.name
}

output "load_balancer_arn_suffix" {
  description = "The LoadBalancer dimension of the ALB's CloudWatch metrics."
  value       = aws_lb.main.arn_suffix
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "orchestrator_security_group_id" {
  value = aws_security_group.orchestrator.id
}

output "vpc_id" {
  value = aws_vpc.main.id
}
