#!/usr/bin/env bash
# Require the job that moves the Stage 12 v2 default after the checks to have
# succeeded exactly when Stage 12 was requested, and to have been skipped
# otherwise.
# Required env vars: REQUESTED ("true" when Stage 12 was requested) and
# DEFAULT_RESULT (the publish_stage12_v2_default job result).

set -euo pipefail

if [[ "${REQUESTED}" == "true" ]]; then
  test "${DEFAULT_RESULT}" = "success"
else
  test "${DEFAULT_RESULT}" = "skipped"
fi
