#!/usr/bin/env bash
# ECS native blue/green, shown twice:
#   good: a new image tag goes to green, answers test traffic (x-steadfast-green: 1) while
#         production still reaches blue, bakes for 2 minutes, then takes production.
#   bad:  a revision whose CALL_LOG_TABLE points at a table that does not exist, so /ready
#         fails. Green never becomes healthy, never gets production traffic, and ECS rolls
#         back by itself.
#
# Usage: scripts/bluegreen_demo.sh [good|bad|both]     (default both)
# Pass:  good ends SUCCESSFUL with production on the new version; bad ends
#        ROLLBACK_SUCCESSFUL with production never leaving the old version.
#
# The new tag is the running image re-tagged in ECR (same digest, no rebuild), so the demo
# takes minutes, not a build. Revisions registered here are deregistered at the end; the
# service keeps running its current one (ECS allows that), and teardown.sh sweeps any left.
set -euo pipefail
cd "$(dirname "$0")/.."

mode="${1:-both}"
eval "$(scripts/aws_env.sh)"
tf() { terraform -chdir=infra output -raw "$1"; }
cluster="$(tf cluster_name)"
service="$(tf orchestrator_service)"
stamp() { date -u +%H:%M:%S; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl_health() { # [extra header...] -> version, or "-" if no answer
  local args=(-s --max-time 5 -H "x-steadfast-key: ${STEADFAST_API_KEY}")
  [[ -n "${STEADFAST_CA_BUNDLE:-}" ]] && args+=(--cacert "$STEADFAST_CA_BUNDLE")
  for h in "$@"; do args+=(-H "$h"); done
  curl "${args[@]}" "${STEADFAST_URL}/health" |
    .venv/bin/python -c 'import json,sys
try: print(json.load(sys.stdin).get("version", "-"))
except Exception: print("-")'
}
prod_version() { curl_health; }
test_version() { curl_health "x-steadfast-green: 1"; }

current_td() {
  aws ecs describe-services --cluster "$cluster" --services "$service" \
    --query 'services[0].taskDefinition' --output text
}

# Register a copy of the running revision: new image tag, SERVICE_VERSION, and optional
# environment overrides (NAME=value). Prints the new revision's ARN.
register_revision() {
  local image="$1" version="$2"
  shift 2
  aws ecs describe-task-definition --task-definition "$(current_td)" --include TAGS \
    --output json >"$tmp/current.json"
  .venv/bin/python - "$tmp/current.json" "$image" "$version" "$@" >"$tmp/new.json" <<'EOF'
import json, sys
src = json.load(open(sys.argv[1]))
td, tags = src["taskDefinition"], src.get("tags", [])
image, version, overrides = sys.argv[2], sys.argv[3], dict(a.split("=", 1) for a in sys.argv[4:])
overrides["SERVICE_VERSION"] = version
container = td["containerDefinitions"][0]
container["image"] = image
env = {e["name"]: e["value"] for e in container.get("environment", [])}
env.update(overrides)
container["environment"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
keep = ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
        "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
        "runtimePlatform", "ephemeralStorage")
out = {k: td[k] for k in keep if k in td}
if tags:
    out["tags"] = tags
print(json.dumps(out))
EOF
  local arn
  arn="$(aws ecs register-task-definition --cli-input-json "file://$tmp/new.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
  echo "$arn" >>"$tmp/registered" # called in $(...): a bash array would not survive
  echo "$arn"
}

# Same image, new tag: copy the manifest under another tag (tags are immutable).
retag_image() {
  local image="$1" new_tag="$2" repo tag manifest media
  repo="${image%:*}"
  repo="${repo#*/}"
  tag="${image##*:}"
  manifest="$(aws ecr batch-get-image --repository-name "$repo" --image-ids imageTag="$tag" \
    --query 'images[0].imageManifest' --output text)"
  media="$(aws ecr batch-get-image --repository-name "$repo" --image-ids imageTag="$tag" \
    --query 'images[0].imageManifestMediaType' --output text)"
  aws ecr put-image --repository-name "$repo" --image-tag "$new_tag" \
    --image-manifest "$manifest" --image-manifest-media-type "$media" >/dev/null
  echo "${image%:*}:${new_tag}"
}

# Start a deployment of $1 and follow it to the end. Prints lifecycle stages and, at each
# change, which version production and test traffic reach. Sets $final_status.
deploy_and_watch() {
  local td="$1" since arn last="" status stage line
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  aws ecs update-service --cluster "$cluster" --service "$service" --task-definition "$td" \
    --query 'service.serviceName' --output text >/dev/null
  arn=""
  for _ in $(seq 1 30); do
    arn="$(aws ecs list-service-deployments --cluster "$cluster" --service "$service" \
      --created-at "after=${since}" --query 'serviceDeployments[0].serviceDeploymentArn' \
      --output text)"
    [[ -n "$arn" && "$arn" != "None" ]] && break
    sleep 2
  done
  local deadline=$((SECONDS + 1800))
  while :; do
    read -r status stage < <(aws ecs describe-service-deployments \
      --service-deployment-arns "$arn" \
      --query 'serviceDeployments[0].[status, lifecycleStage]' --output text)
    line="${status} ${stage}"
    if [[ "$line" != "$last" ]]; then
      echo "  $(stamp) ${status} / ${stage}   production -> $(prod_version)   test header -> $(test_version)"
      last="$line"
    fi
    case "$status" in
    SUCCESSFUL | STOPPED | ROLLBACK_SUCCESSFUL | ROLLBACK_FAILED) break ;;
    esac
    if ((SECONDS > deadline)); then
      status="TIMEOUT"
      break
    fi
    sleep 10
  done
  final_status="$status"
}

# The service rolls back any deployment while one of its deployment alarms is in ALARM, so
# a deploy started right after an outage drill would roll back for reasons unrelated to it.
wait_for_alarms_ok() {
  local names firing deadline=$((SECONDS + ${ALARM_WAIT_S:-900}))
  names="$(aws ecs describe-services --cluster "$cluster" --services "$service" \
    --query 'services[0].deploymentConfiguration.alarms.alarmNames' --output text)"
  [[ -z "$names" || "$names" == "None" ]] && return 0
  while :; do
    # shellcheck disable=SC2086 # one alarm name per word
    firing="$(aws cloudwatch describe-alarms --alarm-names $names --state-value ALARM \
      --query 'MetricAlarms[].AlarmName' --output text)"
    [[ -z "$firing" ]] && return 0
    if ((SECONDS > deadline)); then
      echo "$(stamp) deployment alarms still firing: ${firing}; not deploying"
      exit 1
    fi
    echo "$(stamp) waiting for deployment alarms to clear: ${firing}"
    sleep 30
  done
}

result=0
wait_for_alarms_ok
base_td="$(current_td)"
base_image="$(aws ecs describe-task-definition --task-definition "$base_td" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text)"
echo "$(stamp) production is on version $(prod_version)"

if [[ "$mode" == "good" || "$mode" == "both" ]]; then
  new_tag="${base_image##*:}-bg$(date -u +%H%M%S)"
  echo "$(stamp) GOOD: deploying image tag ${new_tag} through blue/green"
  good_td="$(register_revision "$(retag_image "$base_image" "$new_tag")" "$new_tag")"
  deploy_and_watch "$good_td"
  now="$(prod_version)"
  if [[ "$final_status" == "SUCCESSFUL" && "$now" == "$new_tag" ]]; then
    echo "$(stamp) GOOD: PASS (production on ${now})"
  else
    echo "$(stamp) GOOD: FAIL (status ${final_status}, production on ${now})"
    result=1
  fi
fi

if [[ "$mode" == "bad" || "$mode" == "both" ]]; then
  before="$(prod_version)"
  running_image="$(aws ecs describe-task-definition --task-definition "$(current_td)" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text)"
  echo "$(stamp) BAD: deploying a revision whose call log table does not exist"
  bad_td="$(register_revision "$running_image" "bad-$(date -u +%H%M%S)" \
    "CALL_LOG_TABLE=$(tf call_log_table)-does-not-exist")"
  deploy_and_watch "$bad_td"
  now="$(prod_version)"
  if [[ "$final_status" == "ROLLBACK_SUCCESSFUL" && "$now" == "$before" ]]; then
    echo "$(stamp) BAD: PASS (rolled back; production stayed on ${now})"
  else
    echo "$(stamp) BAD: FAIL (status ${final_status}, production on ${now})"
    result=1
  fi
fi

touch "$tmp/registered"
while read -r arn; do
  aws ecs deregister-task-definition --task-definition "$arn" >/dev/null
done <"$tmp/registered"
exit "$result"
