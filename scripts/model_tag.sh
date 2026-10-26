#!/usr/bin/env bash
# Print the model image tag: a hash of model/ (code, Dockerfile, requirements) plus the
# tier, e.g. 3f2a9c1d0b7e-cpu.
#
# Why not the git SHA like the other images: the model image is ~1 GB and slow to build,
# and orchestrator changes should not force a rebuild. A content hash changes only when
# the model server itself does, so an unchanged model is never rebuilt or re-pushed.
# Usage: scripts/model_tag.sh [cpu|gpu]   (default: MODEL_COMPUTE or cpu)
set -euo pipefail
cd "$(dirname "$0")/.."

tier="${1:-${MODEL_COMPUTE:-cpu}}"
hash="$(find model -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | LC_ALL=C sort |
  while read -r f; do shasum -a 256 "$f"; done | shasum -a 256 | cut -c1-12)"
echo "${hash}-${tier}"
