#!/usr/bin/env bash
# Require the Stage 12 v2 jobs to have run exactly when they were requested:
# all skipped when not requested, all successful when requested.
# Required env vars: REQUESTED, CONFIGURATION_RESULT, DEPLOYMENT_RESULT,
# PUBLICATION_RESULT (GitHub job results).

set -euo pipefail

if [[ "${REQUESTED}" != "true" ]]; then
  test "${CONFIGURATION_RESULT}" = "skipped"
  test "${DEPLOYMENT_RESULT}" = "skipped"
  test "${PUBLICATION_RESULT}" = "skipped"
  exit 0
fi
test "${CONFIGURATION_RESULT}" = "success"
test "${DEPLOYMENT_RESULT}" = "success"
test "${PUBLICATION_RESULT}" = "success"
