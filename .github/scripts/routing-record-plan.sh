#!/usr/bin/env bash
# Record the kept routing plan in the durable routing journal as an
# unfinished publication, before the plan is published.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/routing-plan/plan.json, written by routing-plan.sh or
# routing-plan-latest.sh.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" \
  --record-plan "${RUNNER_TEMP}/routing-plan/plan.json"
