#!/usr/bin/env bash
# Autoscaling demo: replay one morning campaign window (07:45-10:15) against the deployed
# service at speed 6 (25 real minutes), report, wait for scale-in, export the evidence.
#
# Usage: scripts/load_aws.sh
# Env:   SPEED (6), START (07:45), HOURS (2.5), CONCURRENCY (300), LOAD_SEED (42),
#        LABEL (aws-window), WAIT_SCALE_IN (1: wait up to 40 min for the floor again)
#
# Writes results/run-*.json (report), results/aws-<label>-*.json (CloudWatch series) and
# results/aws-<label>-scaling-*.json (every scale-out/in with its reason).
# Exits non-zero if any call is unaccounted.
set -euo pipefail
cd "$(dirname "$0")/.."

speed="${SPEED:-6}"
label="${LABEL:-aws-window}"
eval "$(scripts/aws_env.sh)"
tf() { terraform -chdir=infra output -raw "$1"; }
cluster="$(tf cluster_name)"
service="$(tf orchestrator_service)"
started=$SECONDS

desired() {
  aws ecs describe-services --cluster "$cluster" --services "$service" \
    --query 'services[0].desiredCount' --output text
}
echo "tasks before: $(desired)"

.venv/bin/python -m loadgen.generate --url "$STEADFAST_URL" --speed "$speed" \
  --start "${START:-07:45}" --duration-hours "${HOURS:-2.5}" --seed "${LOAD_SEED:-42}" \
  --concurrency "${CONCURRENCY:-300}" --no-prom-metrics
set +e
.venv/bin/python -m loadgen.report --url "$STEADFAST_URL" --label "$label" --no-prom-metrics
status=$?
set -e

if [[ "${WAIT_SCALE_IN:-1}" == "1" ]]; then
  floor="$(aws application-autoscaling describe-scalable-targets --service-namespace ecs \
    --resource-ids "service/${cluster}/${service}" \
    --query 'ScalableTargets[0].MinCapacity' --output text)"
  # Target tracking scales in only after 15 minutes below target, then in steps.
  echo "waiting for scale-in to ${floor} tasks (up to 40 min)"
  deadline=$((SECONDS + 2400))
  while (($(desired) > floor && SECONDS < deadline)); do
    echo "  $(date -u +%H:%M:%S) desired $(desired)"
    sleep 60
  done
  echo "tasks after: $(desired)"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
aws application-autoscaling describe-scaling-activities --service-namespace ecs \
  --resource-id "service/${cluster}/${service}" --max-items 50 \
  --query 'ScalingActivities[].{start:StartTime,end:EndTime,status:StatusCode,what:Description,why:Cause}' \
  --output json >"results/aws-${label}-scaling-${stamp}.json"
echo "wrote results/aws-${label}-scaling-${stamp}.json"
MINUTES=$(((SECONDS - started) / 60 + 5)) scripts/aws_results.sh "$label"
exit "$status"
