#!/usr/bin/env bash
# Multi-AZ drill: lose every orchestrator task in one Availability Zone in the middle of a
# load run, and show that no call is lost or even errors at the client.
#
# Why it should pass: stop-task sends SIGTERM, so a task finishes its in-flight calls
# (SHUTDOWN_DRAIN_S) while the ALB stops sending it new ones (deregistration delay); the
# other AZ keeps serving; ECS starts replacements, and AZ rebalancing spreads them out again.
#
# Usage: scripts/az_drill.sh
# Env:   AZ (default: the AZ of the first running task), KILL_AFTER_S (90),
#        SPEED (6), START (08:30), HOURS (0.75), LOAD_SEED (11), LABEL (az-drill)
# Pass:  loadgen http_errors = 0 and unaccounted = 0 (exit 1 otherwise).
set -euo pipefail
cd "$(dirname "$0")/.."

kill_after="${KILL_AFTER_S:-90}"
label="${LABEL:-az-drill}"
eval "$(scripts/aws_env.sh)"
tf() { terraform -chdir=infra output -raw "$1"; }
cluster="$(tf cluster_name)"
service="$(tf orchestrator_service)"
started=$SECONDS
stamp() { date -u +%H:%M:%S; }

running_tasks() {
  aws ecs list-tasks --cluster "$cluster" --service-name "$service" \
    --desired-status RUNNING --query 'taskArns' --output text
}

# "AZ count" lines, e.g. "ap-south-1a 2".
tasks_per_az() {
  local arns
  arns="$(running_tasks)"
  [[ -z "$arns" ]] && return 0
  # shellcheck disable=SC2086 # word-splitting the ARN list is intended
  aws ecs describe-tasks --cluster "$cluster" --tasks $arns \
    --query 'tasks[?lastStatus==`RUNNING`].availabilityZone' --output text |
    tr '\t' '\n' | sort | uniq -c | awk '{print $2, $1}'
}

tasks_in_az() {
  local arns
  arns="$(running_tasks)"
  [[ -z "$arns" ]] && return 0
  # shellcheck disable=SC2086
  aws ecs describe-tasks --cluster "$cluster" --tasks $arns \
    --query "tasks[?availabilityZone=='$1'].taskArn" --output text
}

desired() {
  aws ecs describe-services --cluster "$cluster" --services "$service" \
    --query 'services[0].desiredCount' --output text
}

echo "$(stamp) tasks per AZ before:"
tasks_per_az | sed 's/^/  /'

echo "$(stamp) starting load (campaign window ${START:-08:30}, speed ${SPEED:-6})"
.venv/bin/python -m loadgen.generate --url "$STEADFAST_URL" --speed "${SPEED:-6}" \
  --start "${START:-08:30}" --duration-hours "${HOURS:-0.75}" --seed "${LOAD_SEED:-11}" \
  --concurrency 300 --no-prom-metrics &
load_pid=$!

sleep "$kill_after"
az="${AZ:-$(tasks_per_az | head -1 | cut -d' ' -f1)}"
victims="$(tasks_in_az "$az")"
echo "$(stamp) stopping every orchestrator task in ${az}: $(wc -w <<<"$victims" | tr -d ' ') task(s)"
for arn in $victims; do
  aws ecs stop-task --cluster "$cluster" --task "$arn" --reason "az drill: ${az} lost" \
    --query 'task.lastStatus' --output text >/dev/null
done

echo "$(stamp) waiting for ECS to replace them"
deadline=$((SECONDS + 600))
while :; do
  # Count tasks that are actually RUNNING; a just-placed replacement is PROVISIONING first.
  now_running="$(tasks_per_az | awk '{n += $2} END {print n + 0}')"
  want="$(desired)"
  echo "  $(stamp) running ${now_running} / desired ${want}"
  ((now_running >= want)) && break
  ((SECONDS > deadline)) && { echo "  replacements did not start within 10 min"; break; }
  sleep 15
done
echo "$(stamp) tasks per AZ after recovery:"
tasks_per_az | sed 's/^/  /'

wait "$load_pid"
set +e
.venv/bin/python -m loadgen.report --url "$STEADFAST_URL" --label "$label" --no-prom-metrics
set -e

report="$(ls -t results/run-*.json | head -1)"
read -r http_errors unaccounted < <(.venv/bin/python -c \
  'import json,sys; r=json.load(open(sys.argv[1])); print(r["http_errors"], r["unaccounted"])' \
  "$report")

lb="$(tf load_balancer_arn_suffix)"
window_start="$(date -u -v-"$(((SECONDS - started) / 60 + 2))"M +%Y-%m-%dT%H:%M:00Z 2>/dev/null ||
  date -u -d "-$(((SECONDS - started) / 60 + 2)) min" +%Y-%m-%dT%H:%M:00Z)"
alb_5xx="$(aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_ELB_5XX_Count --dimensions Name=LoadBalancer,Value="$lb" \
  --start-time "$window_start" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 60 --statistics Sum --query 'sum(Datapoints[].Sum)' --output text)"
[[ "$alb_5xx" == "None" ]] && alb_5xx=0

echo "AZ drill (${az}): http_errors=${http_errors} unaccounted=${unaccounted} alb_5xx=${alb_5xx}"
if [[ "$http_errors" == "0" && "$unaccounted" == "0" ]]; then
  echo "PASS"
else
  echo "FAIL"
  exit 1
fi
