#!/usr/bin/env bash
# Plan the Modal routing change that adds the routes of the executor app this
# deployment deployed (its policyengine.py version and its US and UK model
# versions) and its bundle manifest, leaving latest; routing-plan-latest.sh
# plans moving latest after the checks. Keep the plan for the record, publish,
# complete and restore steps. The plan names its publication by run, attempt
# and environment.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, APP_NAME, POLICYENGINE_VERSION,
# POLICYENGINE_US_VERSION, POLICYENGINE_UK_VERSION, RELEASE_ENVIRONMENT, and
# the runner's RUNNER_TEMP, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT and
# GITHUB_OUTPUT. The Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Writes: $RUNNER_TEMP/routing-plan/plan.json, and the planner's output to
# $RUNNER_TEMP/routing-plan.out.
# Outputs: before_sha256, after_sha256 and advances_latest in GITHUB_OUTPUT.

set -euo pipefail

mkdir -p "${RUNNER_TEMP}/routing-plan"
uv run --frozen python -m src.modal.utils.update_version_registry \
  --app-name "${APP_NAME}" \
  --policyengine-version "${POLICYENGINE_VERSION}" \
  --us-version "${POLICYENGINE_US_VERSION}" \
  --uk-version "${POLICYENGINE_UK_VERSION}" \
  --environment "${MODAL_ENVIRONMENT}" \
  --plan-out "${RUNNER_TEMP}/routing-plan/plan.json" \
  --publication-id "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${RELEASE_ENVIRONMENT}" \
  | tee "${RUNNER_TEMP}/routing-plan.out"
grep -E '^(before_sha256|after_sha256|advances_latest)=' \
  "${RUNNER_TEMP}/routing-plan.out" >> "$GITHUB_OUTPUT"
