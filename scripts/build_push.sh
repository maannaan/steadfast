#!/usr/bin/env bash
# Build the orchestrator and mock images for linux/amd64 (Fargate X86_64) and push to ECR.
#
# Usage: scripts/build_push.sh [TAG]     (default: scripts/image_tag.sh)
# Env:   AWS_REGION (default ap-south-1), NAME (default steadfast), COMPONENTS
#        (default "orchestrator mock"; "model" builds the model tier locally, tagged by
#        scripts/model_tag.sh instead of TAG; scripts/build_model.sh builds it in AWS,
#        which is what model_on.sh uses), MODEL_COMPUTE (cpu | gpu, default cpu)
#        BUILD_NETWORK=host  run build steps on the Docker VM's network instead of the
#                            default bridge (works around slow pip downloads on some
#                            Docker Desktop setups)
#
# The account ID is looked up at runtime from STS and never written to the repo.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-ap-south-1}"
NAME="${NAME:-steadfast}"
TAG="${1:-$(scripts/image_tag.sh)}"
COMPONENTS="${COMPONENTS:-orchestrator mock}"

account="$(aws sts get-caller-identity --query Account --output text)"
registry="${account}.dkr.ecr.${REGION}.amazonaws.com"
aws ecr get-login-password --region "$REGION" |
  docker login --username AWS --password-stdin "$registry" >/dev/null

for component in $COMPONENTS; do
  tag="$TAG"
  build_args=""
  case "$component" in
    orchestrator) dockerfile=service/Dockerfile ;;
    mock) dockerfile=mock/Dockerfile ;;
    model)
      # Content-hash tag per tier (CUDA vs slim base); see scripts/model_tag.sh.
      dockerfile=model/Dockerfile
      tag="$(scripts/model_tag.sh "${MODEL_COMPUTE:-cpu}")"
      build_args="--build-arg COMPUTE=${MODEL_COMPUTE:-cpu}"
      ;;
    *) echo "unknown component: $component" >&2; exit 1 ;;
  esac
  repo="${NAME}-${component}"
  # Tags are immutable: if this commit is already pushed, reuse it instead of failing.
  if aws ecr describe-images --region "$REGION" --repository-name "$repo" \
      --image-ids imageTag="$tag" >/dev/null 2>&1; then
    echo "${repo}:${tag} already in ECR, skipping build"
    continue
  fi
  echo "building ${repo}:${tag}"
  # shellcheck disable=SC2086 # build_args is deliberately word-split
  docker buildx build --platform linux/amd64 --provenance=false \
    ${BUILD_NETWORK:+--network "$BUILD_NETWORK"} $build_args \
    -f "$dockerfile" -t "${registry}/${repo}:${tag}" --push .
done
