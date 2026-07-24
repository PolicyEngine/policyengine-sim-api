#!/bin/bash
# Run the artifact precompute app against a Modal environment.
# Usage: ./modal-precompute.sh <modal-environment> [force]
# Run from projects/policyengine-simulation-executor (like modal-deploy-app.sh).
# Required env vars: POLICYENGINE_ARTIFACT_BUCKET (GCS artifact store bucket).
# Outputs: sets manifest_digest in GITHUB_OUTPUT, parsed from the app's
# MANIFEST_DIGEST= stdout contract (the last matching line wins; Modal may
# append its own output after the entrypoint returns).

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
FORCE="${2:-false}"

if [ -z "${POLICYENGINE_ARTIFACT_BUCKET:-}" ]; then
  echo "POLICYENGINE_ARTIFACT_BUCKET is required (the GCS artifact store bucket)." >&2
  echo "Set it as a repository Actions variable, or export it for local runs." >&2
  exit 1
fi

RUN_COMMAND=(uv run modal run --env="$MODAL_ENV" src/modal/precompute_app.py)
if [[ "$FORCE" == "true" || "$FORCE" == "1" ]]; then
    RUN_COMMAND+=(--force)
fi

echo "========================================"
echo "Precomputing artifacts in Modal environment: $MODAL_ENV"
echo "  Bucket: gs://${POLICYENGINE_ARTIFACT_BUCKET}"
echo "  Force recompute: ${FORCE}"
echo "========================================"

OUTPUT_LOG="$(mktemp)"
trap 'rm -f "$OUTPUT_LOG"' EXIT

"${RUN_COMMAND[@]}" 2>&1 | tee "$OUTPUT_LOG"

DIGEST_LINE="$(tr -d '\r' < "$OUTPUT_LOG" | grep '^MANIFEST_DIGEST=' | tail -n 1 || true)"
if [ -z "$DIGEST_LINE" ]; then
  echo "Precompute output contained no MANIFEST_DIGEST= line; cannot export a manifest digest." >&2
  exit 1
fi

MANIFEST_DIGEST="${DIGEST_LINE#MANIFEST_DIGEST=}"
echo "Manifest digest: $MANIFEST_DIGEST"
echo "manifest_digest=$MANIFEST_DIGEST" >> "$GITHUB_OUTPUT"
