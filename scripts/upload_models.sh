#!/usr/bin/env bash
# Download the open-weight models for one tier from Hugging Face (once, cached in
# model-weights/, gitignored) and sync them to s3://<models_bucket>/models/<tier>/.
#
# Usage: scripts/upload_models.sh [cpu|gpu]   (default: MODEL_COMPUTE or cpu)
#
# Models and licences (all allow publishing this repo under Apache-2.0):
#   cpu: Systran/faster-whisper-small (MIT), Qwen/Qwen2.5-1.5B-Instruct-GGUF Q4_K_M
#        (Apache-2.0)                                                       ~1.7 GB
#   gpu: mobiuslabsgmbh/faster-whisper-large-v3-turbo (MIT),
#        bartowski/Mistral-7B-Instruct-v0.3-GGUF Q4_K_M (Apache-2.0)        ~6.0 GB
#   both: rhasspy/piper-voices en_US-ljspeech-medium (LJ Speech, public domain)
set -euo pipefail
cd "$(dirname "$0")/.."

tier="${1:-${MODEL_COMPUTE:-cpu}}"
cache="model-weights/${tier}"
bucket="$(terraform -chdir=infra output -raw models_bucket)"

# hf REPO FILE [DEST]: resumable download of one file to $cache/DEST.
hf() {
  local dest="${cache}/${3:-$2}"
  if [[ -s "$dest" ]]; then return; fi
  mkdir -p "$(dirname "$dest")"
  echo "downloading $1/$2"
  curl -fL --retry 5 -C - -o "${dest}.part" "https://huggingface.co/$1/resolve/main/$2"
  mv "${dest}.part" "$dest"
}

case "$tier" in
  cpu)
    for f in config.json model.bin tokenizer.json vocabulary.txt; do
      hf Systran/faster-whisper-small "$f" "faster-whisper-small/$f"
    done
    hf Qwen/Qwen2.5-1.5B-Instruct-GGUF qwen2.5-1.5b-instruct-q4_k_m.gguf
    ;;
  gpu)
    for f in config.json model.bin preprocessor_config.json tokenizer.json vocabulary.json; do
      hf mobiuslabsgmbh/faster-whisper-large-v3-turbo "$f" "faster-whisper-large-v3-turbo/$f"
    done
    hf bartowski/Mistral-7B-Instruct-v0.3-GGUF Mistral-7B-Instruct-v0.3-Q4_K_M.gguf
    ;;
  *) echo "tier must be cpu or gpu" >&2; exit 1 ;;
esac
voice=en/en_US/ljspeech/medium
hf rhasspy/piper-voices "${voice}/en_US-ljspeech-medium.onnx" en_US-ljspeech-medium.onnx
hf rhasspy/piper-voices "${voice}/en_US-ljspeech-medium.onnx.json" en_US-ljspeech-medium.onnx.json
hf rhasspy/piper-voices "${voice}/MODEL_CARD" en_US-ljspeech-medium.MODEL_CARD

echo "syncing ${cache}/ to s3://${bucket}/models/${tier}/"
aws s3 sync "${cache}/" "s3://${bucket}/models/${tier}/" --exclude '*.part' --only-show-errors
aws s3 ls --summarize --human-readable --recursive "s3://${bucket}/models/${tier}/" | tail -2
