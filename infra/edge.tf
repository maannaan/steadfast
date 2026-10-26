# How clients reach the service, and who may.
#
# Why not plain HTTP with an IP allowlist (the first design): from many client networks,
# plain HTTP does not leave from the client's own IP. macOS iCloud Private Relay and
# corporate proxies send it from rotating relay addresses, which locked the operator out,
# and reviewers connect from IPs we cannot know in advance. HTTPS is never relayed.
#
# 1. Default: an HTTPS listener on the ALB with a self-signed certificate for the ALB's
#    DNS name (no domain needed). Clients verify it by pinning: `terraform output
#    alb_certificate_pem` is the only certificate they trust (curl --cacert), so the
#    connection is encrypted AND authenticated without a public CA.
# 2. enable_cloudfront = true (once AWS Support has verified the account for CloudFront):
#    an https://*.cloudfront.net URL with a publicly trusted certificate.
#
# Either way, every request must carry x-steadfast-key. The ALB listener rule checks it
# and answers 403 otherwise, before any task sees the request. The key is generated here,
# exists only in the (gitignored) Terraform state, and is read with
# `terraform output -raw access_key`.

locals {
  access_key_header = "x-steadfast-key"
}

resource "random_password" "access_key" {
  length  = 40
  special = false # ALB header conditions treat * and ? as wildcards
}

# ---------- HTTPS on the ALB (self-signed, pinned by clients) ----------

resource "tls_private_key" "alb" {
  algorithm = "RSA"
  rsa_bits  = 2048 # what ACM accepts for imported certificates
}

resource "tls_self_signed_cert" "alb" {
  private_key_pem       = tls_private_key.alb.private_key_pem
  dns_names             = [aws_lb.main.dns_name]
  validity_period_hours = 24 * 30 # sessions are short; re-created on every deploy
  allowed_uses          = ["digital_signature", "key_encipherment", "server_auth"]

  subject {
    common_name  = "${var.name}-alb"
    organization = "Steadfast demo (self-signed)"
  }
}

resource "aws_acm_certificate" "alb" {
  private_key      = tls_private_key.alb.private_key_pem
  certificate_body = tls_self_signed_cert.alb.cert_pem

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.alb.arn

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = "{\"detail\": \"missing or invalid x-steadfast-key\"}"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "with_key" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 1

  condition {
    http_header {
      http_header_name = local.access_key_header
      values           = [random_password.access_key.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.orchestrator.arn
  }
}

# ---------- Optional: CloudFront (publicly trusted HTTPS) ----------

# CloudFront -> ALB over HTTP on port 80, reachable only from CloudFront's address range
# (security.tf) and still gated by the key. Production would put an ACM certificate for a
# real domain on the ALB and use https-only to the origin.
resource "aws_lb_listener" "http_from_cloudfront" {
  count             = var.enable_cloudfront ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = "{\"detail\": \"missing or invalid x-steadfast-key\"}"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "cloudfront_with_key" {
  count        = var.enable_cloudfront ? 1 : 0
  listener_arn = aws_lb_listener.http_from_cloudfront[0].arn
  priority     = 1

  condition {
    http_header {
      http_header_name = local.access_key_header
      values           = [random_password.access_key.result]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.orchestrator.arn
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  count = var.enable_cloudfront ? 1 : 0
  name  = "Managed-CachingDisabled"
}

# Forwards all viewer headers (including the key), query strings and the body.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  count = var.enable_cloudfront ? 1 : 0
  name  = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "main" {
  count           = var.enable_cloudfront ? 1 : 0
  enabled         = true
  comment         = "${var.name} call service"
  is_ipv6_enabled = true
  price_class     = "PriceClass_200" # includes India edge locations; PriceClass_100 does not

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.main.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # A 6-turn demo call holds the request ~27 s; 60 s is the maximum without a quota
      # increase. A call that outlives it is still recorded (the client sees 504,
      # retries, and gets the stored result).
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer[0].id
    compress                 = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  depends_on = [aws_lb_listener_rule.cloudfront_with_key]
}
