#!/usr/bin/env bash
# Route all stable Cloud Run service traffic to the requested retained
# revision, unless it already serves it.
# Required env vars: SERVICE, SIMULATION_ENTRYPOINT_TARGET_REVISION,
# SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION, and the other
# SIMULATION_ENTRYPOINT_* variables set-cloud-run-simulation-entry-revision.sh
# reads. gcloud uses the job's Google credentials.

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${SIMULATION_ENTRYPOINT_TARGET_REVISION}" == "${SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION}" ]]; then
  echo "${SERVICE} already serves ${SIMULATION_ENTRYPOINT_TARGET_REVISION}."
else
  "${script_directory}/set-cloud-run-simulation-entry-revision.sh"
fi
