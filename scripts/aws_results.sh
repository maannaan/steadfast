#!/usr/bin/env bash
# Export the CloudWatch metrics of the last MINUTES (default 60) to results/aws-<label>-*.json.
# Run it before destroying the stack. Usage: scripts/aws_results.sh [LABEL]
set -euo pipefail
cd "$(dirname "$0")/.."

tf() { terraform -chdir=infra output -raw "$1"; }
exec .venv/bin/python scripts/aws_results.py \
  --label "${1:-run}" --minutes "${MINUTES:-60}" \
  --region "$(tf region)" \
  --lb "$(tf load_balancer_arn_suffix)" --cluster "$(tf cluster_name)" \
  --orchestrator "$(tf orchestrator_service)" --replay "$(tf replay_service)" \
  --queue "$(tf dlq_queue_name)"
