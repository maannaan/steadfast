#!/usr/bin/env bash
# Print the image tag for the current checkout: the git short SHA, so every image in ECR
# maps to a commit. Uncommitted changes get "-dirty-<timestamp>": ECR tags are immutable,
# and an image built from local edits must never pose as the committed code.
set -euo pipefail
cd "$(dirname "$0")/.."

sha="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "${sha}-dirty-$(date -u +%Y%m%d%H%M%S)"
else
  echo "$sha"
fi
