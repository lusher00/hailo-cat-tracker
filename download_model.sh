#!/bin/bash
# Fetch a YOLOv8s .hef compiled for the Hailo-8L.
# The model is ~23MB and is gitignored, so this runs once per checkout.
set -e

MODEL="${1:-yolov8s}"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/${MODEL}.hef"

if [ -f "${DEST}" ]; then
    echo "${DEST} already exists — nothing to do."
    exit 0
fi

# 1. Already on disk from hailo-all?
for CANDIDATE in \
    "/usr/share/hailo-models/${MODEL}_h8l.hef" \
    "/usr/share/hailo-models/${MODEL}.hef"; do
    if [ -f "${CANDIDATE}" ]; then
        echo "Found ${CANDIDATE} — copying."
        cp "${CANDIDATE}" "${DEST}"
        echo "Saved to ${DEST}"
        exit 0
    fi
done

# 2. Otherwise pull from the Hailo model zoo S3 bucket.
URL="https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.14.0/hailo8l/${MODEL}.hef"
echo "Downloading ${URL} ..."
if command -v wget >/dev/null 2>&1; then
    wget -O "${DEST}" "${URL}"
else
    curl -fL -o "${DEST}" "${URL}"
fi

echo "Saved to ${DEST}"
echo "Note: if you're on a Hailo-8 (not 8L), swap 'hailo8l' for 'hailo8' in the URL."
