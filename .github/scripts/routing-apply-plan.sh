#!/usr/bin/env bash
# Publish the kept routing plan to Modal.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/routing-plan/plan.json, written by routing-plan.sh.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" \
  --apply-plan "${RUNNER_TEMP}/routing-plan/plan.json"
