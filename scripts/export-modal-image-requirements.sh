#!/usr/bin/env bash
# Export the Modal image dependency groups from uv.lock to pinned
# requirements files the image definitions install from. Rerun after any
# relock; make update does this automatically, and a unit test
# (tests/test_modal_image_requirements.py) fails CI if the exports drift
# from the lock.
set -euo pipefail

cd "$(dirname "$0")/../projects/policyengine-api-simulation"
mkdir -p requirements

for group in modal-simulation-image modal-gateway-image; do
    uv export \
        --only-group "$group" \
        --frozen \
        --no-hashes \
        --no-annotate \
        --output-file "requirements/$group.txt" \
        --quiet
done
