#!/usr/bin/env bash
# Create or update the whole AWS stack: ECR repositories, images, then everything else.
#
# Usage: scripts/deploy.sh            (terraform asks for confirmation before each apply)
#        AUTO_APPROVE=1 scripts/deploy.sh
# Env:   IMAGE_TAG (default: git SHA), BUILD_NETWORK (see build_push.sh)
#
# Writes infra/session.auto.tfvars (gitignored) so later plain `terraform plan/destroy`
# in infra/ use the same inputs.
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${IMAGE_TAG:-$(scripts/image_tag.sh)}"
APPROVE=""
[[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE="-auto-approve"

printf 'image_tag = "%s"\n' "$TAG" >infra/session.auto.tfvars
echo "image tag ${TAG}"

terraform -chdir=infra init -input=false >/dev/null
# Bootstrap: images can only be pushed once their repositories exist, and the services
# cannot start until the images are there.
terraform -chdir=infra apply -input=false $APPROVE -target=aws_ecr_repository.this
scripts/build_push.sh "$TAG"
terraform -chdir=infra apply -input=false $APPROVE

echo "waiting for the services to become stable..."
cluster="$(terraform -chdir=infra output -raw cluster_name)"
service="$(terraform -chdir=infra output -raw orchestrator_service)"
aws ecs wait services-stable --cluster "$cluster" --services "$service"
echo "ready: $(terraform -chdir=infra output -raw service_url)"
echo "access key: terraform -chdir=infra output -raw access_key"
