#!/usr/bin/env bash
# Drain the AWS dead-letter queue: start one replay worker, wait until the queue is empty,
# stop the worker. Run it once the cause of the failures is fixed (the DLQ alarm says when).
#
# Usage: scripts/replay_aws.sh
# Env:   MAX_WAIT_S (default 1800). The worker is scaled back to 0 even on Ctrl-C or timeout.
#
# "Empty" counts in-flight messages too: a replay that failed stays hidden for the queue's
# visibility timeout (300 s) and is then retried, so the drain waits for it.
set -euo pipefail
cd "$(dirname "$0")/.."

tf() { terraform -chdir=infra output -raw "$1"; }
cluster="$(tf cluster_name)"
service="$(tf replay_service)"
queue="$(tf dlq_queue_url)"
max_wait="${MAX_WAIT_S:-1800}"

depth() {
  aws sqs get-queue-attributes --queue-url "$queue" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
    --query 'Attributes.[ApproximateNumberOfMessages, ApproximateNumberOfMessagesNotVisible]' \
    --output text
}

scale() {
  aws ecs update-service --cluster "$cluster" --service "$service" --desired-count "$1" \
    --query 'service.desiredCount' --output text >/dev/null
}

trap 'echo "stopping the replay worker"; scale 0' EXIT

read -r waiting inflight <<<"$(depth)"
echo "DLQ before: ${waiting} waiting, ${inflight} in flight"
if [[ "$waiting" == "0" && "$inflight" == "0" ]]; then
  echo "nothing to replay"
  exit 0
fi

scale 1
start=$SECONDS
empty_checks=0
# SQS counts are approximate; require two consecutive empty readings 30 s apart.
while ((empty_checks < 2)); do
  sleep 30
  read -r waiting inflight <<<"$(depth)"
  echo "t+$((SECONDS - start))s: ${waiting} waiting, ${inflight} in flight"
  if [[ "$waiting" == "0" && "$inflight" == "0" ]]; then
    empty_checks=$((empty_checks + 1))
  else
    empty_checks=0
  fi
  if ((SECONDS - start > max_wait)); then
    echo "queue not empty after ${max_wait}s; see the replay worker logs" >&2
    exit 1
  fi
done
echo "DLQ drained in $((SECONDS - start))s"
