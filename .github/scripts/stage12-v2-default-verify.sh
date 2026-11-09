#!/usr/bin/env bash
# Refuse unless the Stage 12 v2 manifest is what the kept default plan wrote
# and a reader resolves requests that pin no version to the plan's default.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/stage12-v2-default/plan.json, written by
# stage12-v2-default-move.sh.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_v2_version_manifest \
  --environment "${MODAL_ENVIRONMENT}" \
  --manifest-name simulation-api-v2-version-manifest \
  --verify-plan "${RUNNER_TEMP}/stage12-v2-default/plan.json"
