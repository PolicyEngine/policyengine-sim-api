#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
expected_revision="${2:-}"
# Stable hostnames can briefly continue serving the revision that was active
# immediately before a traffic update. Supplying that revision as argument 3
# enables bounded retries for only that documented transition state.
previous_revision="${3:-}"
# Optional production identity: when set, /versions must show that the
# `latest` route serves this policyengine.py version with this UK model and
# certified dataset SHA-256 (from the deployed executor's locked bundle).
expected_policyengine_version="${EXPECTED_POLICYENGINE_VERSION:-}"
expected_uk_model_version="${EXPECTED_UK_MODEL_VERSION:-}"
expected_uk_dataset_sha256="${EXPECTED_UK_DATASET_SHA256:-}"
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
versions_body="$(mktemp)"
trap 'rm -f "${health_headers}" "${health_body}" "${versions_body}"' EXIT

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
curl --fail --silent --show-error "${base_url}/versions" --output "${versions_body}"
jq -e 'type == "object"' "${versions_body}" >/dev/null

if [ -n "${expected_policyengine_version}" ]; then
  if ! jq -e --arg v "${expected_policyengine_version}" '
    .policyengine[$v] != null and .policyengine.latest == .policyengine[$v]
  ' "${versions_body}" >/dev/null; then
    printf '%s: latest does not route to policyengine %s (latest=%s)\n' \
      "${base_url}" "${expected_policyengine_version}" \
      "$(jq -r '.policyengine.latest // "none"' "${versions_body}")" >&2
    exit 1
  fi
  if ! jq -e --arg v "${expected_policyengine_version}" '
    .bundles[$v].uk.policyengine_version == $v
  ' "${versions_body}" >/dev/null; then
    printf '%s: /versions has no UK bundle identity for policyengine %s\n' \
      "${base_url}" "${expected_policyengine_version}" >&2
    exit 1
  fi
  if [ -n "${expected_uk_model_version}" ] && ! jq -e \
    --arg v "${expected_policyengine_version}" --arg m "${expected_uk_model_version}" '
    .bundles[$v].uk.model_version == $m and .uk.latest == .policyengine[$v]
  ' "${versions_body}" >/dev/null; then
    printf '%s: policyengine %s does not serve policyengine-uk %s as latest (got %s)\n' \
      "${base_url}" "${expected_policyengine_version}" "${expected_uk_model_version}" \
      "$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.model_version // "none"' "${versions_body}")" >&2
    exit 1
  fi
  if [ -n "${expected_uk_dataset_sha256}" ] && ! jq -e \
    --arg v "${expected_policyengine_version}" --arg d "${expected_uk_dataset_sha256}" '
    .bundles[$v].uk.dataset_sha256 == $d
  ' "${versions_body}" >/dev/null; then
    printf '%s: the UK dataset SHA-256 served for policyengine %s is %s, expected %s\n' \
      "${base_url}" "${expected_policyengine_version}" \
      "$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.dataset_sha256 // "none"' "${versions_body}")" \
      "${expected_uk_dataset_sha256}" >&2
    exit 1
  fi
  printf '%s serves policyengine %s, policyengine-uk %s, UK dataset %s\n' \
    "${base_url}" "${expected_policyengine_version}" \
    "$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.model_version' "${versions_body}")" \
    "$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.dataset_sha256 // "unrecorded"' "${versions_body}")"
fi
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
