#!/usr/bin/env bash
# Check that the release sentinel computed in-process names this deployment's
# bundle: its policyengine.py version, UK model version and UK dataset
# digest, with at least one case. jq prints true or false, and exits 1 on
# false.
# Required env vars: EXPECTED_POLICYENGINE_VERSION, EXPECTED_UK_MODEL_VERSION,
# EXPECTED_UK_DATASET_SHA256, and the runner's RUNNER_TEMP.
# Reads: $RUNNER_TEMP/release-sentinel.json.

set -euo pipefail

jq -e \
  --arg policyengine_version "${EXPECTED_POLICYENGINE_VERSION}" \
  --arg uk_model_version "${EXPECTED_UK_MODEL_VERSION}" \
  --arg uk_dataset_sha256 "${EXPECTED_UK_DATASET_SHA256}" '
    .policyengine_version == $policyengine_version
    and .uk.model_version == $uk_model_version
    and .uk.dataset_sha256 == $uk_dataset_sha256
    and (.cases | length) > 0
  ' "${RUNNER_TEMP}/release-sentinel.json"
