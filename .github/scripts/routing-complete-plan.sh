#!/usr/bin/env bash
# Complete the kept routing plan's publication in the durable routing journal,
# refusing unless it is the unfinished publication and its state is live.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/routing-plan/plan.json, written by routing-plan.sh or
# routing-plan-latest.sh.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" \
  --complete-plan "${RUNNER_TEMP}/routing-plan/plan.json"
