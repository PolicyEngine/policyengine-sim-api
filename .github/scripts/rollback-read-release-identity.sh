#!/usr/bin/env bash
# Read the release identity latest now routes to from the routing registry.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's GITHUB_OUTPUT. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Outputs: policyengine_version, app_name, uk_model_version and
# uk_dataset_sha256 (when the release recorded one) in GITHUB_OUTPUT.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" --print-identity \
  | grep -E '^(policyengine_version|app_name|uk_model_version|uk_dataset_sha256)=' \
  | tee -a "$GITHUB_OUTPUT"
