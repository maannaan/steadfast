# Service Connect gives the orchestrator stable names (http://mock:8081, later
# http://model:8080) with client-side load balancing and retries on connection failure,
# without an internal load balancer per backend (~$0.03/h each). An HTTP-only namespace
# needs no Route 53 private zone.
resource "aws_service_discovery_http_namespace" "main" {
  name        = var.name
  description = "Service Connect namespace for Steadfast"
}
