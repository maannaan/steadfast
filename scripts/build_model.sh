#!/usr/bin/env bash
# Build and push the model image on a one-shot EC2 instance in the VPC (see infra/build.tf
# for why not locally or in CodeBuild). Skipped if the content-hash tag is already in ECR.
#
# Usage: scripts/build_model.sh [cpu|gpu]   (default: MODEL_COMPUTE or cpu)
# Needs the stack applied (launch template + bucket). Prints the build output on failure.
set -euo pipefail
cd "$(dirname "$0")/.."

tier="${1:-${MODEL_COMPUTE:-cpu}}"
tag="$(scripts/model_tag.sh "$tier")"
region="${AWS_REGION:-ap-south-1}"
tf() { terraform -chdir=infra output -raw "$1"; }
repo="${NAME:-steadfast}-model"

if aws ecr describe-images --repository-name "$repo" --image-ids imageTag="$tag" >/dev/null 2>&1; then
  echo "${repo}:${tag} already in ECR, skipping build"
  exit 0
fi

bucket="$(tf models_bucket)"
template="$(tf builder_launch_template)"
repo_uri="$(aws ecr describe-repositories --repository-names "$repo" \
  --query 'repositories[0].repositoryUri' --output text)"

zip_file="$(mktemp -d)/model-src.zip"
zip -qr -X "$zip_file" model -x '*/__pycache__/*' '*.pyc'
key="build/model-${tag}.zip"
aws s3 cp --only-show-errors "$zip_file" "s3://${bucket}/${key}"

# Runs as root on first boot. The EXIT trap uploads the build log and powers off
# (= terminates) whether the build worked or not.
log_key="build/logs/model-${tag}.log"
user_data="$(cat <<EOF
#!/bin/bash
exec > >(tee /var/log/steadfast-build.log) 2>&1
set -euxo pipefail
export AWS_DEFAULT_REGION=${region}
trap 'aws s3 cp --only-show-errors /var/log/steadfast-build.log "s3://${bucket}/${log_key}"; shutdown -h now' EXIT
shutdown -h +60
dnf install -y -q docker
systemctl start docker
mkdir -p /root/src && cd /root/src
aws s3 cp --only-show-errors "s3://${bucket}/${key}" src.zip
python3 -m zipfile -e src.zip .
aws ecr get-login-password | docker login --username AWS --password-stdin "${repo_uri%%/*}"
docker build --build-arg COMPUTE=${tier} -f model/Dockerfile -t "${repo_uri}:${tag}" .
docker push "${repo_uri}:${tag}"
echo STEADFAST_BUILD_OK
EOF
)"

instance="$(aws ec2 run-instances --launch-template "LaunchTemplateId=${template}" \
  --user-data "$user_data" --query 'Instances[0].InstanceId' --output text)"
echo "builder ${instance} building ${repo}:${tag} (about 10 minutes)"

for _ in $(seq 1 80); do
  sleep 30
  state="$(aws ec2 describe-instances --instance-ids "$instance" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)"
  echo "  $(date -u +%H:%M:%S) ${state}"
  [[ "$state" == "terminated" || "$state" == "shutting-down" ]] && break
done

if aws ecr describe-images --repository-name "$repo" --image-ids imageTag="$tag" >/dev/null 2>&1; then
  echo "pushed ${repo}:${tag}"
  exit 0
fi
echo "build failed; last lines of s3://${bucket}/${log_key}:" >&2
aws s3 cp --only-show-errors "s3://${bucket}/${log_key}" - | tail -40 >&2 || true
aws ec2 terminate-instances --instance-ids "$instance" >/dev/null || true
exit 1
