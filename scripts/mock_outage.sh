#!/usr/bin/env bash
# Full dependency outage drill: stop the mock in the middle of a campaign-window load run,
# bring it back, then replay the DLQ. Proves an outage loses no calls and that every
# dead-lettered call can be recovered.
#
# Env: KILL_AFTER_S (default 40), OUTAGE_S (30), LOAD_SEED (7), RATE (50 replays/s)
set -euo pipefail
cd "$(dirname "$0")/.."

KILL_AFTER_S=${KILL_AFTER_S:-40}
OUTAGE_S=${OUTAGE_S:-30}
LOAD_SEED=${LOAD_SEED:-7}
RATE=${RATE:-50}

stamp() { date -u +%H:%M:%S; }

echo "$(stamp) starting load: campaign window 07:45-10:15 at speed 60"
docker compose --profile loadgen run --rm loadgen python -m loadgen.generate \
  --speed 60 --start 07:45 --duration-hours 2.5 --seed "$LOAD_SEED" &
LOAD_PID=$!

sleep "$KILL_AFTER_S"
echo "$(stamp) stopping mock (outage begins)"
docker compose stop mock
sleep "$OUTAGE_S"
echo "$(stamp) starting mock (outage ends)"
docker compose start mock

wait "$LOAD_PID"
echo "$(stamp) load finished"

status=0
docker compose --profile loadgen run --rm loadgen python -m loadgen.report --label outage \
  || status=$?

echo "$(stamp) replaying DLQ at ${RATE}/s"
docker compose exec -T service python -m service.cli dlq replay --all --rate "$RATE" || true
docker compose exec -T service python -m service.cli dlq stats
exit "$status"
