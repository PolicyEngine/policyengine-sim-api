#!/usr/bin/env bash
# Restore the routing state the kept plan replaced, only while the live state
# is what the plan published, and record its publication as restored.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/routing-plan/plan.json, written by routing-plan-latest.sh.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" \
  --restore-plan "${RUNNER_TEMP}/routing-plan/plan.json"
