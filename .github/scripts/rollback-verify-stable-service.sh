#!/usr/bin/env bash
# Verify that the stable service serves the restored release: a rollback to
# a requested version must have made it latest, and the stable URL must
# serve the expected revision and the whole identity latest routes to.
# Required env vars: POLICYENGINE_VERSION (blank when restoring a deployment
# run's plans), EXPECTED_POLICYENGINE_VERSION, STABLE_URL, EXPECTED_REVISION,
# PREVIOUS_REVISION, and the EXPECTED_* identity variables
# cloud-run-simulation-entry-smoke.sh reads.

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${POLICYENGINE_VERSION}" && "${EXPECTED_POLICYENGINE_VERSION}" != "${POLICYENGINE_VERSION}" ]]; then
  echo "latest routes to ${EXPECTED_POLICYENGINE_VERSION}, not ${POLICYENGINE_VERSION}" >&2
  exit 1
fi
"${script_directory}/cloud-run-simulation-entry-smoke.sh" \
  "${STABLE_URL}" "${EXPECTED_REVISION}" "${PREVIOUS_REVISION}"
