#!/bin/bash
# Require every deployment phase to have succeeded, and the promotion to have
# run exactly when it was requested, so a skipped phase cannot report a
# successful partial deployment. An existing-stack deployment also requires
# its routing publication and a journal finalization that completed it.
# Required env vars: PROMOTION_REQUESTED, EXISTING_STACK_REQUESTED,
# ENTRYPOINT_RESULT, ROUTING_RESULT, INTEGRATION_RESULT,
# AUTHENTICATED_TEST_RESULT, PROMOTION_RESULT, FINALIZE_RESULT (GitHub job
# results) and FINALIZE_OUTCOME (the finalize_routing outcome output).

set -euo pipefail

test "${ENTRYPOINT_RESULT}" = "success"
test "${INTEGRATION_RESULT}" = "success"
test "${AUTHENTICATED_TEST_RESULT}" = "success"
if [[ "${PROMOTION_REQUESTED}" = "true" ]]; then
  test "${PROMOTION_RESULT}" = "success"
else
  test "${PROMOTION_RESULT}" = "skipped"
fi
if [[ "${EXISTING_STACK_REQUESTED}" = "true" ]]; then
  test "${ROUTING_RESULT}" = "success"
  test "${FINALIZE_RESULT}" = "success"
  test "${FINALIZE_OUTCOME}" = "completed"
fi
