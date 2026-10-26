locals {
  # model is pushed in Phase D; creating the empty repository now costs nothing.
  components = toset(["orchestrator", "mock", "model"])
}

resource "aws_ecr_repository" "this" {
  for_each = local.components
  name     = "${var.name}-${each.key}"

  # Immutable tags: a git SHA always means the same image, so a rollback really restores
  # the code that was running before.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # Sessions end with terraform destroy; repositories holding images must still go.
  force_delete = true
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
