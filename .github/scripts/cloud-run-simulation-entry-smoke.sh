#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
expected_revision="${2:-}"
# Stable hostnames can briefly continue serving the revision that was active
# immediately before a traffic update. Supplying that revision as argument 3
# enables bounded retries for only that documented transition state.
previous_revision="${3:-}"
routing_max_attempts="${SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS:-24}"
routing_retry_delay_seconds="${SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS:-5}"

if ! [[ "${routing_max_attempts}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS must be a positive integer\n' >&2
  exit 2
fi
if ! [[ "${routing_retry_delay_seconds}" =~ ^[0-9]+$ ]]; then
  printf 'SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS must be a non-negative integer\n' >&2
  exit 2
fi

health_headers="$(mktemp)"
health_body="$(mktemp)"
trap 'rm -f "${health_headers}" "${health_body}"' EXIT

attempt=1
while true; do
  curl --fail --silent --show-error \
    --dump-header "${health_headers}" \
    --output "${health_body}" \
    "${base_url}/health"
  jq -e '.status == "healthy"' "${health_body}" >/dev/null

  if [ -z "${expected_revision}" ]; then
    break
  fi

  actual_revision="$(
    awk '
      tolower($1) == "x-policyengine-simulation-revision:" {
        gsub("\r", "", $2)
        print $2
      }
    ' "${health_headers}" |
      tail -n 1
  )"

  if [ "${actual_revision}" = "${expected_revision}" ]; then
    break
  fi

  if [ -z "${previous_revision}" ]; then
    printf 'Expected revision %s at %s, received %s\n' \
      "${expected_revision}" "${base_url}" "${actual_revision:-no revision header}" >&2
    exit 1
  fi

  if [ "${actual_revision}" != "${previous_revision}" ]; then
    printf 'Unexpected revision at %s: expected %s or previous revision %s, received %s\n' \
      "${base_url}" "${expected_revision}" "${previous_revision}" \
      "${actual_revision:-no revision header}" >&2
    exit 1
  fi

  if [ "${attempt}" -ge "${routing_max_attempts}" ]; then
    printf '%s did not serve revision %s after %s attempts; it still served previous revision %s\n' \
      "${base_url}" "${expected_revision}" "${routing_max_attempts}" \
      "${previous_revision}" >&2
    exit 1
  fi

  printf '%s still served previous revision %s; retrying target revision %s (%s/%s)\n' \
    "${base_url}" "${previous_revision}" "${expected_revision}" \
    "${attempt}" "${routing_max_attempts}" >&2
  sleep "${routing_retry_delay_seconds}"
  attempt=$((attempt + 1))
done

curl --fail --silent --show-error "${base_url}/ready" |
  jq -e '.status == "ready"' >/dev/null
curl --fail --silent --show-error "${base_url}/versions" >/dev/null
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
