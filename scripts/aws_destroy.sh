#!/usr/bin/env bash
# Destroy everything Terraform created. Run at the end of every session.
#
# Usage: scripts/aws_destroy.sh   (terraform asks for confirmation)
#        AUTO_APPROVE=1 scripts/aws_destroy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

APPROVE=""
[[ "${AUTO_APPROVE:-0}" == "1" ]] && APPROVE="-auto-approve"

if [[ ! -f infra/session.auto.tfvars ]]; then
  # Inputs only matter for creating resources; any value lets destroy evaluate.
  printf 'image_tag = "destroy"\n' >infra/session.auto.tfvars
fi
terraform -chdir=infra init -input=false >/dev/null
# CloudFront must be disabled and propagated before it can be deleted: allow ~5-15 min.
terraform -chdir=infra destroy -input=false $APPROVE
echo "remaining resources in state: $(terraform -chdir=infra state list | wc -l | tr -d ' ')"
