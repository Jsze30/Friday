#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p models
cd models

MODEL="vosk-model-small-en-us-0.15"
if [ -d "$MODEL" ]; then
  echo "$MODEL already present"
  exit 0
fi

echo "Downloading $MODEL..."
curl -L -o "$MODEL.zip" "https://alphacephei.com/vosk/models/$MODEL.zip"
unzip -q "$MODEL.zip"
rm "$MODEL.zip"
echo "Installed models/$MODEL"
