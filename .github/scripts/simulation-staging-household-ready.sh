#!/usr/bin/env bash
# Require the staging household check to have succeeded in a staging
# deployment, and to have been skipped in any other.
# Required env vars: REQUESTED ("true" in a staging deployment) and
# CHECK_RESULT (the staging_household_check job result).

set -euo pipefail

if [[ "${REQUESTED}" == "true" ]]; then
  test "${CHECK_RESULT}" = "success"
else
  test "${CHECK_RESULT}" = "skipped"
fi
