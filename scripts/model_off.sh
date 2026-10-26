#!/usr/bin/env bash
# Point the orchestrator back at the mock and stop the model task; the capacity provider
# then terminates the instance, so the model tier costs nothing again.
#
# Usage: scripts/model_off.sh     Env: AUTO_APPROVE=1
set -euo pipefail
cd "$(dirname "$0")/.."

APPROVE=""
[[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE="-auto-approve"
tfvars=infra/model.auto.tfvars
if [[ ! -f "$tfvars" ]]; then
  echo "model tier is not on (no ${tfvars})"
  exit 0
fi

# Keep the tier and image tag (the task definition stays valid); turn everything else off.
grep -E '^(model_compute|model_image_tag) ' "$tfvars" >"${tfvars}.new" || true
printf 'model_desired_count = 0\npipeline_target     = "mock"\n' >>"${tfvars}.new"
mv "${tfvars}.new" "$tfvars"
terraform -chdir=infra apply -input=false $APPROVE

cluster="$(terraform -chdir=infra output -raw cluster_name)"
aws ecs wait services-stable --cluster "$cluster" \
  --services "$(terraform -chdir=infra output -raw orchestrator_service)" \
  "$(terraform -chdir=infra output -raw model_service)"
echo "model tier off; the instance terminates within a few minutes"
