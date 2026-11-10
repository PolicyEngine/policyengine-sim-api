#!/usr/bin/env bash
# Read every key of the separate Stage 12 v2 manifest in one Modal environment.
# Modal expires a Dict entry after 7 days without a read or write, and the
# entrypoint reads the manifest only for a Stage 12 request, so a scheduled
# workflow runs this daily. Fails when there is no manifest (never published,
# or already expired) or the stored one is invalid. Reads only. Stage 12's own
# mechanism: the v1 routing scripts are separate.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT. The Modal client reads MODAL_TOKEN_ID
# and MODAL_TOKEN_SECRET.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_v2_version_manifest \
  --environment "${MODAL_ENVIRONMENT}" \
  --manifest-name simulation-api-v2-version-manifest \
  --refresh
