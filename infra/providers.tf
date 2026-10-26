# Credentials come from the standard AWS chain (AWS_PROFILE, env vars or SSO); nothing
# account-specific is written here.
provider "aws" {
  region = var.region

  # Every resource carries Project=steadfast, so costs and the teardown check can be
  # scoped to this project and never touch anything else in the account.
  default_tags {
    tags = {
      Project   = var.name
      Challenge = "predixion-fde"
      ManagedBy = "terraform"
    }
  }
}

# Cost Explorer's API lives only in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = var.name
      Challenge = "predixion-fde"
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
