#!/usr/bin/env bash
# End-of-session teardown with proof: stop the model tier, destroy every Terraform
# resource, then ask AWS directly that nothing tagged Project=steadfast is left.
#
# Writes results/teardown.log (destroy output) and results/teardown-verify.txt (PASS/FAIL),
# both with account IDs redacted because results/ is committed.
#
# Usage: scripts/teardown.sh     Env: AUTO_APPROVE=1
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p results
region="${AWS_REGION:-ap-south-1}"
# The account ID appears in ARNs, queue URLs, registry hostnames and budget IDs; replace
# the exact value (looked up, never written down) rather than guessing at formats.
account="$(aws sts get-caller-identity --query Account --output text)"
redact() { sed -e "s/${account}/<account>/g"; }

# Output goes through a pipe, which would hide Terraform's own prompt, so confirm here.
if [[ "${AUTO_APPROVE:-0}" != "1" ]]; then
  read -r -p "Destroy the whole Steadfast stack in ${region}? Type 'yes': " answer
  [[ "$answer" == "yes" ]] || { echo "aborted"; exit 1; }
fi
export AUTO_APPROVE=1

{
  echo "teardown started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # Draining the model task first lets the capacity provider release its instance
  # cleanly instead of deleting the ASG under a running task.
  if [[ -f infra/model.auto.tfvars ]] && terraform -chdir=infra output -raw model_service >/dev/null 2>&1; then
    scripts/model_off.sh
  fi
  scripts/aws_destroy.sh
  echo "teardown finished $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} 2>&1 | redact | tee results/teardown.log

.venv/bin/python scripts/teardown_verify.py --region "$region" \
  --project "${PROJECT:-steadfast}" --out results/teardown-verify.txt
