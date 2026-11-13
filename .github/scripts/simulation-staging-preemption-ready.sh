#!/bin/bash
# Require the staging preemption check to have succeeded in a staging
# deployment, and to have been skipped in any other.
# Required env vars: REQUESTED ("true" in a staging deployment) and
# CHECK_RESULT (the staging_preemption_check job result).

set -euo pipefail

if [[ "${REQUESTED}" == "true" ]]; then
  test "${CHECK_RESULT}" = "success"
else
  test "${CHECK_RESULT}" = "skipped"
fi
