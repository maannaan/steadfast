terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0" # 6.x: ECS native blue/green (deployment_configuration.strategy)
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # Local state on purpose: one operator, short apply-test-destroy sessions, nothing left
  # running overnight. State files are gitignored (they contain resource IDs and ARNs).
  # A team setup would use an S3 backend with DynamoDB locking instead.
}
