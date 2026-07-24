#!/bin/bash
# Record a deployed environment's manifest digest in the artifact store.
# Usage: ./modal-record-deployment.sh <environment> <manifest-digest>
# Run from projects/policyengine-simulation-executor (like modal-deploy-app.sh).
# Required env vars: POLICYENGINE_ARTIFACT_BUCKET (GCS artifact store bucket),
# plus GCP credentials (GCP_CREDENTIALS_JSON) for the store write.

set -euo pipefail

ENVIRONMENT="${1:?Environment required (e.g. beta or prod)}"
MANIFEST_DIGEST="${2:?Manifest digest required (the precompute job output)}"

if [ -z "${POLICYENGINE_ARTIFACT_BUCKET:-}" ]; then
  echo "POLICYENGINE_ARTIFACT_BUCKET is required (the GCS artifact store bucket)." >&2
  exit 1
fi

echo "Recording deployment marker for ${ENVIRONMENT} (manifest ${MANIFEST_DIGEST})"
uv run python -m src.modal.utils.record_deployment \
  --environment "$ENVIRONMENT" \
  --manifest-digest "$MANIFEST_DIGEST"
