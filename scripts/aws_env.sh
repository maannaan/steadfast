#!/usr/bin/env bash
# Print the environment for talking to the deployed service, from Terraform outputs:
#   eval "$(scripts/aws_env.sh)"
# Sets STEADFAST_URL, STEADFAST_API_KEY and STEADFAST_CA_BUNDLE (the ALB's self-signed
# certificate, written to infra/alb-cert.pem, gitignored). The loadgen, report, CLI and
# smoke test all read these.
set -euo pipefail
cd "$(dirname "$0")/.."

tf() { terraform -chdir=infra output -raw "$1"; }
url="$(tf service_url)"
cert="$PWD/infra/alb-cert.pem"
tf alb_certificate_pem >"$cert"

echo "export STEADFAST_URL='${url}'"
echo "export STEADFAST_API_KEY='$(tf access_key)'"
if [[ "$url" == *.elb.amazonaws.com ]]; then
  echo "export STEADFAST_CA_BUNDLE='${cert}'"
else
  echo "unset STEADFAST_CA_BUNDLE" # CloudFront: publicly trusted certificate
fi
