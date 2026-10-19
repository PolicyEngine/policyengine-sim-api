#!/usr/bin/env bash
# Validate the installed bundles, secrets, datasets, calculations and Stage 12
# cache of the deployed v2 workers, and keep the result for manifest
# publication.
# Usage: stage12-validate-v2-workers.sh <modal-environment> <cache-manifest-sha256>
# Run from projects/policyengine-simulation-executor.
# Required env vars: RUNNER_TEMP, GITHUB_OUTPUT.
# Outputs: file (the validation result path) in GITHUB_OUTPUT.

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
CACHE_MANIFEST_SHA256="${2:?Stage 12 cache manifest digest required}"

output="${RUNNER_TEMP}/stage12-worker-validation.json"
uv run python -m src.modal.utils.validate_v2_worker_release \
  --environment "${MODAL_ENV}" \
  --cache-manifest-sha256 "${CACHE_MANIFEST_SHA256}" \
  --output "${output}"
echo "file=${output}" >> "$GITHUB_OUTPUT"
