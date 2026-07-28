#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
expected_revision="${2:-}"

health_headers="$(mktemp)"
health_body="$(mktemp)"
trap 'rm -f "${health_headers}" "${health_body}"' EXIT

curl --fail --silent --show-error \
  --dump-header "${health_headers}" \
  --output "${health_body}" \
  "${base_url}/health"
jq -e '.status == "healthy"' "${health_body}" >/dev/null

if [ -n "${expected_revision}" ]; then
  actual_revision="$(
    awk '
      tolower($1) == "x-policyengine-simulation-revision:" {
        gsub("\r", "", $2)
        print $2
      }
    ' "${health_headers}" |
      tail -n 1
  )"
  if [ "${actual_revision}" != "${expected_revision}" ]; then
    printf 'Expected revision %s at %s, received %s\n' \
      "${expected_revision}" "${base_url}" "${actual_revision:-no revision header}" >&2
    exit 1
  fi
fi

curl --fail --silent --show-error "${base_url}/ready" |
  jq -e '.status == "ready"' >/dev/null
curl --fail --silent --show-error "${base_url}/versions" >/dev/null
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
