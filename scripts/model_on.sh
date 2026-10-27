#!/usr/bin/env bash
# Switch the orchestrator from the mock to the open-weight model tier:
# build the model image in AWS (skipped if unchanged), upload weights (skipped if
# present), start one model instance + task, point the orchestrator at http://model:8080.
#
# Usage: scripts/model_on.sh [cpu|gpu]   (default: MODEL_COMPUTE or cpu)
# Env:   AUTO_APPROVE=1
#
# Writes infra/model.auto.tfvars (gitignored), which later applies keep using until
# scripts/model_off.sh. Cost while on: see docs/COST.md (CPU c7i.xlarge ~$0.18/h,
# GPU g5.xlarge ~$1.21/h, plus EBS).
set -euo pipefail
cd "$(dirname "$0")/.."

tier="${1:-${MODEL_COMPUTE:-cpu}}"
tag="$(scripts/model_tag.sh "$tier")"
APPROVE=""
[[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE="-auto-approve"

# write_tfvars DESIRED TARGET
write_tfvars() {
  cat >infra/model.auto.tfvars <<EOF
model_compute       = "${tier}"
model_image_tag     = "${tag}"
model_desired_count = $1
pipeline_target     = "$2"
EOF
}

# Pass 1 creates the weights bucket, the builder template and the (still idle) model tier;
# the task needs its image and weights before it starts, so it is switched on in pass 2.
write_tfvars 0 mock
terraform -chdir=infra apply -input=false $APPROVE

# The image builds in AWS while the weights upload from here: independent, so overlap them.
scripts/build_model.sh "$tier" &
build_pid=$!
scripts/upload_models.sh "$tier"
wait "$build_pid"
write_tfvars 1 model
terraform -chdir=infra apply -input=false $APPROVE

cluster="$(terraform -chdir=infra output -raw cluster_name)"
echo "waiting for the model to load and pass its health check (several minutes)..."
# One waiter run gives up after 10 minutes; a cold start (new instance, ~1 GB image,
# 1.6-6 GB of weights, warm-up turn) can take longer, so allow two.
model_service="$(terraform -chdir=infra output -raw model_service)"
aws ecs wait services-stable --cluster "$cluster" --services "$model_service" ||
  aws ecs wait services-stable --cluster "$cluster" --services "$model_service"
aws ecs wait services-stable --cluster "$cluster" \
  --services "$(terraform -chdir=infra output -raw orchestrator_service)"
echo "model tier on (${tier}, image ${tag})"
