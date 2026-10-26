#!/usr/bin/env bash
# Smoke test through the public URL: access control, health, readiness, and one call
# recorded and looked up. Exits non-zero on the first failure.
#
# Env: STEADFAST_URL, STEADFAST_API_KEY, STEADFAST_CA_BUNDLE (default: scripts/aws_env.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -n "${STEADFAST_API_KEY:-}" ]] || eval "$(scripts/aws_env.sh)"
URL="$STEADFAST_URL"
TLS=()
[[ -n "${STEADFAST_CA_BUNDLE:-}" ]] && TLS=(--cacert "$STEADFAST_CA_BUNDLE")
event_id="smoke-$(date -u +%Y%m%dT%H%M%S)-$RANDOM"
req() { curl -fsS --max-time 60 ${TLS[@]+"${TLS[@]}"} -H "x-steadfast-key: ${STEADFAST_API_KEY}" "$@"; }

echo "GET  $URL/health without key (expect 403)"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 ${TLS[@]+"${TLS[@]}"} "$URL/health")"
[[ "$code" == "403" ]] || { echo "expected 403, got $code"; exit 1; }

echo "GET  $URL/health"
req "$URL/health"; echo
echo "GET  $URL/ready"
req "$URL/ready"; echo

echo "POST $URL/calls ($event_id)"
req -X POST "$URL/calls" -H 'content-type: application/json' -d @- <<EOF
{"event_id": "$event_id", "campaign_id": "CAMP-SMOKE", "borrower_bucket": "31-60 DPD",
 "product_type": "credit_card", "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
echo

# Read back through the ALB: likely a different task, so this proves the log is shared.
echo "GET  $URL/calls/$event_id"
req "$URL/calls/$event_id"; echo
echo "POST $URL/calls/lookup"
lookup="$(req -X POST "$URL/calls/lookup" -H 'content-type: application/json' \
  -d "{\"event_ids\": [\"$event_id\"]}")"
echo "$lookup"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d["missing"]==[] else 1)' "$lookup"
echo "SMOKE PASS"
