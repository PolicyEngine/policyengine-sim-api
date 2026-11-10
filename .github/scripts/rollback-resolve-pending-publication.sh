#!/usr/bin/env bash
# Print the target's unfinished routing publication from the durable routing
# journal, then restore what it replaced, as a publication whose release
# checks failed. Needs no artifact from the deployment run.
# Run from projects/policyengine-simulation-executor.
# Required env vars: TARGET, MODAL_ENVIRONMENT. The Modal client reads
# MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" --print-pending-plan
uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" --finalize-publication \
  --release-environment "${TARGET}" --gates-passed false
