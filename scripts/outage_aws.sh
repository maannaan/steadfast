#!/usr/bin/env bash
# Dependency drill on AWS, under load:
#   MODE=slow (default): the mock answers slowly (LATENCY_MU raised), so attempts time out
#     and turns retry. Per-turn p99 crosses 3x baseline and steadfast-latency-p99 fires;
#     calls that run out of deadline are dead-lettered, none are lost.
#   MODE=down: the mock is scaled to zero. Attempts fail fast, the circuit breaker opens,
#     and calls are dead-lettered at once instead of piling up.
# Then the mock is restored and the alarm returns to OK. Replay the DLQ afterwards with
# scripts/replay_aws.sh.
#
# Usage: scripts/outage_aws.sh
# Env:   MODE (slow), HOLD_S (420: the alarm needs 3 of 5 one-minute datapoints, and EMF
#        metrics arrive a minute late), SLOW_MU (8.0: lognormal median ~3 s, past the 2.5 s
#        attempt timeout), START (09:00), SPEED (6), LOAD_SEED (13), LABEL (outage-<mode>)
# Pass:  loadgen unaccounted = 0 (exit 1 otherwise).
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${MODE:-slow}"
hold="${HOLD_S:-420}"
label="${LABEL:-outage-${mode}}"
eval "$(scripts/aws_env.sh)"
tf() { terraform -chdir=infra output -raw "$1"; }
cluster="$(tf cluster_name)"
mock="${cluster}-mock"
alarm="${cluster}-latency-p99"
stamp() { date -u +%H:%M:%S; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

alarm_state() {
  aws cloudwatch describe-alarms --alarm-names "$alarm" \
    --query 'MetricAlarms[0].StateValue' --output text
}
mock_td() {
  aws ecs describe-services --cluster "$cluster" --services "$mock" \
    --query 'services[0].taskDefinition' --output text
}

original_td="$(mock_td)"
restore() {
  echo "$(stamp) restoring the mock"
  if [[ "$mode" == "down" ]]; then
    aws ecs update-service --cluster "$cluster" --service "$mock" --desired-count 1 \
      --query 'service.desiredCount' --output text >/dev/null
  else
    aws ecs update-service --cluster "$cluster" --service "$mock" \
      --task-definition "$original_td" --query 'service.serviceName' --output text >/dev/null
  fi
  aws ecs wait services-stable --cluster "$cluster" --services "$mock"
  [[ -n "${slow_td:-}" ]] && aws ecs deregister-task-definition --task-definition "$slow_td" >/dev/null
  echo "$(stamp) mock healthy again"
}

echo "$(stamp) alarm ${alarm}: $(alarm_state)"
echo "$(stamp) starting load (campaign window ${START:-09:00}, speed ${SPEED:-6})"
duration_h="$(.venv/bin/python -c "print(round(($hold + 240) * ${SPEED:-6} / 3600, 2))")"
.venv/bin/python -m loadgen.generate --url "$STEADFAST_URL" --speed "${SPEED:-6}" \
  --start "${START:-09:00}" --duration-hours "$duration_h" --seed "${LOAD_SEED:-13}" \
  --concurrency 300 --no-prom-metrics &
load_pid=$!
sleep 60

if [[ "$mode" == "down" ]]; then
  echo "$(stamp) OUTAGE: mock scaled to 0"
  aws ecs update-service --cluster "$cluster" --service "$mock" --desired-count 0 \
    --query 'service.desiredCount' --output text >/dev/null
else
  echo "$(stamp) DEGRADED: mock latency median ~$(.venv/bin/python -c "import math; print(round(math.exp(${SLOW_MU:-8.0})))") ms"
  aws ecs describe-task-definition --task-definition "$original_td" --include TAGS \
    --output json >"$tmp/td.json"
  .venv/bin/python - "$tmp/td.json" "${SLOW_MU:-8.0}" >"$tmp/slow.json" <<'EOF'
import json, sys
src = json.load(open(sys.argv[1]))
td = src["taskDefinition"]
c = td["containerDefinitions"][0]
env = {e["name"]: e["value"] for e in c.get("environment", [])}
env["LATENCY_MU"] = sys.argv[2]
c["environment"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
keep = ("family", "executionRoleArn", "networkMode", "containerDefinitions",
        "requiresCompatibilities", "cpu", "memory", "runtimePlatform")
out = {k: td[k] for k in keep if k in td}
if src.get("tags"):
    out["tags"] = src["tags"]
print(json.dumps(out))
EOF
  slow_td="$(aws ecs register-task-definition --cli-input-json "file://$tmp/slow.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
  aws ecs update-service --cluster "$cluster" --service "$mock" --task-definition "$slow_td" \
    --query 'service.serviceName' --output text >/dev/null
fi
trap 'restore; rm -rf "$tmp"' EXIT

deadline=$((SECONDS + hold))
last=""
while ((SECONDS < deadline)); do
  state="$(alarm_state)"
  [[ "$state" != "$last" ]] && echo "$(stamp) alarm ${alarm}: ${state}"
  last="$state"
  sleep 20
done

restore
trap 'rm -rf "$tmp"' EXIT
wait "$load_pid"
set +e
.venv/bin/python -m loadgen.report --url "$STEADFAST_URL" --label "$label" --no-prom-metrics
status=$?
set -e

# Once the load stops there are no new datapoints, and CloudWatch keeps judging the last
# breaching minutes for about 8 more minutes before notBreaching takes over (measured).
echo "$(stamp) waiting up to 10 min for the alarm to clear"
deadline=$((SECONDS + 600))
while [[ "$(alarm_state)" == "ALARM" ]] && ((SECONDS < deadline)); do sleep 30; done
echo "$(stamp) alarm ${alarm}: $(alarm_state)"
exit "$status"
