#!/usr/bin/env bash
# Move the separate Stage 12 v2 manifest's default version to the worker this
# deployment published, when it is newer, after every check. The plan (the
# manifest before and after the move, with digests) is kept for the verify and
# restore steps. Stage 12's own mechanism: the v1 routing scripts are separate.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, POLICYENGINE_VERSION, and the runner's
# RUNNER_TEMP and GITHUB_OUTPUT. The Modal client reads MODAL_TOKEN_ID and
# MODAL_TOKEN_SECRET.
# Writes: $RUNNER_TEMP/stage12-v2-default/plan.json, and the publisher's output
# to $RUNNER_TEMP/stage12-v2-default.out.
# Outputs: before_sha256, after_sha256 and release_is_default (whether requests
# that pin no v2 version now reach the release's worker) in GITHUB_OUTPUT.

set -euo pipefail

mkdir -p "${RUNNER_TEMP}/stage12-v2-default"
uv run --frozen python -m src.modal.utils.update_v2_version_manifest \
  --environment "${MODAL_ENVIRONMENT}" \
  --manifest-name simulation-api-v2-version-manifest \
  --move-default "${POLICYENGINE_VERSION}" \
  --plan-out "${RUNNER_TEMP}/stage12-v2-default/plan.json" \
  | tee "${RUNNER_TEMP}/stage12-v2-default.out"
grep -E '^(before_sha256|after_sha256|release_is_default)=' \
  "${RUNNER_TEMP}/stage12-v2-default.out" >> "$GITHUB_OUTPUT"
