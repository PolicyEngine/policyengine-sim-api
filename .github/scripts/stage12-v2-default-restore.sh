#!/usr/bin/env bash
# Write back the Stage 12 v2 manifest the kept default plan replaced, only
# while the stored manifest is what the plan wrote. Without a kept plan this
# attempt's move wrote nothing, so this attempt has nothing to restore. An
# earlier attempt's move may have written part of a move; that attempt's
# plan is its own stage12-v2-default-plan artifact.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, and the runner's RUNNER_TEMP. The
# Modal client reads MODAL_TOKEN_ID and MODAL_TOKEN_SECRET.
# Reads: $RUNNER_TEMP/stage12-v2-default/plan.json, written by
# stage12-v2-default-move.sh.

set -euo pipefail

plan="${RUNNER_TEMP}/stage12-v2-default/plan.json"
if [[ ! -f "${plan}" ]]; then
  echo "This attempt's move kept no v2 default plan, so this attempt wrote nothing: nothing to restore."
  echo "If an earlier attempt's move stopped part-way, restore from that attempt's stage12-v2-default-plan artifact."
  exit 0
fi
uv run --frozen python -m src.modal.utils.update_v2_version_manifest \
  --environment "${MODAL_ENVIRONMENT}" \
  --manifest-name simulation-api-v2-version-manifest \
  --restore-plan "${plan}"
