#!/bin/bash
# Write the Simulation Entrypoint's main URL (its stable Cloud Run URL, which
# reaches the revision that serves traffic) to $GITHUB_OUTPUT as `url`, for
# the v2 Modal app's report_call_ended function.
#
# Environment: PROJECT_ID, REGION, SERVICE, GITHUB_OUTPUT.
set -euo pipefail

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

for variable_name in PROJECT_ID REGION SERVICE GITHUB_OUTPUT; do
  [[ -n "${!variable_name:-}" ]] || fail "${variable_name} is required"
done

url="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)')"
[[ "${url}" =~ ^https://[a-z0-9.-]+$ ]] \
  || fail "${SERVICE} has no https main URL: ${url:-none}"
printf 'url=%s\n' "${url}" >> "${GITHUB_OUTPUT}"
