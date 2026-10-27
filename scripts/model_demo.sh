#!/usr/bin/env bash
# Open-weight model tier under pressure (after scripts/model_on.sh ... model): a short
# burst of calls, more than the CPU model can serve at once, with the model's injected
# faults on. Shows the report (still zero unaccounted) and the model's own record of what
# it rejected and why (model_rejected log events), from CloudWatch Logs Insights.
#
# Usage: scripts/model_demo.sh
# Env:   SPEED (4), START (09:00), HOURS (0.25), CONCURRENCY (64), LOAD_SEED (17),
#        LABEL (model-burst)
set -euo pipefail
cd "$(dirname "$0")/.."

eval "$(scripts/aws_env.sh)"
region="$(terraform -chdir=infra output -raw region)"
target="$(terraform -chdir=infra output -raw pipeline_target)"
[[ "$target" == "model" ]] || { echo "pipeline_target is ${target}: run scripts/model_on.sh first"; exit 2; }
started="$(date +%s)"

.venv/bin/python -m loadgen.generate --url "$STEADFAST_URL" --speed "${SPEED:-4}" \
  --start "${START:-09:00}" --duration-hours "${HOURS:-0.25}" --seed "${LOAD_SEED:-17}" \
  --concurrency "${CONCURRENCY:-64}" --no-prom-metrics
set +e
.venv/bin/python -m loadgen.report --url "$STEADFAST_URL" --label "${LABEL:-model-burst}" \
  --no-prom-metrics
status=$?
set -e

echo "model_rejected events by reason (model log group, this run):"
query_id="$(aws logs start-query --region "$region" --log-group-name /ecs/steadfast/model \
  --start-time "$started" --end-time "$(($(date +%s) + 60))" \
  --query-string 'filter @message like /model_rejected/
    | parse @message /"reason": "(?<reason>[^":]+)/
    | stats count() as rejected by reason | sort rejected desc' \
  --query queryId --output text)"
for _ in $(seq 1 30); do
  sleep 2
  out="$(aws logs get-query-results --region "$region" --query-id "$query_id" --output json)"
  [[ "$(jq -r .status <<<"$out")" == "Complete" ]] && break
done
jq -r '.results[] | map({(.field): .value}) | add | "  \(.reason): \(.rejected)"' <<<"$out"
exit "$status"
