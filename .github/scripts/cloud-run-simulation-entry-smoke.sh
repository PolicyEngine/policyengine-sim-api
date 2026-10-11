#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
expected_revision="${2:-}"
# Stable hostnames can briefly continue serving the revision that was active
# immediately before a traffic update. Supplying that revision as argument 3
# enables bounded retries for only that documented transition state.
previous_revision="${3:-}"
# Optional release identity, checked against /versions, whose route maps hold
# versions as `latest` and app names under each version key:
# - EXPECTED_POLICYENGINE_VERSION: that policyengine.py version has a route
#   and a recorded UK bundle identity;
# - EXPECTED_UK_MODEL_VERSION / EXPECTED_UK_DATASET_SHA256: that bundle's UK
#   model (routed to the same app) and certified dataset SHA-256;
# - EXPECTED_LATEST=true: unpinned requests route to that version and model.
# This is the identity the gateway reports from the routing registry; the
# numerical release sentinel checks what the worker computes.
expected_policyengine_version="${EXPECTED_POLICYENGINE_VERSION:-}"
expected_uk_model_version="${EXPECTED_UK_MODEL_VERSION:-}"
expected_uk_dataset_sha256="${EXPECTED_UK_DATASET_SHA256:-}"
expected_latest="${EXPECTED_LATEST:-}"
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

identity_failure() {
  printf '%s: %s\n' "${base_url}" "$1" >&2
  exit 1
}

if [ -n "${expected_policyengine_version}" ]; then
  jq -e --arg v "${expected_policyengine_version}" '
    (.policyengine[$v] | type == "string" and length > 0)
    and .bundles[$v].uk.policyengine_version == $v
  ' "${versions_body}" >/dev/null ||
    identity_failure "/versions has no route and UK bundle identity for policyengine ${expected_policyengine_version}"

  served_uk_model="$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.model_version // ""' "${versions_body}")"
  if [ -n "${expected_uk_model_version}" ]; then
    [ "${served_uk_model}" = "${expected_uk_model_version}" ] ||
      identity_failure "policyengine ${expected_policyengine_version} records policyengine-uk ${served_uk_model:-none}, expected ${expected_uk_model_version}"
  fi
  jq -e --arg v "${expected_policyengine_version}" --arg m "${served_uk_model}" '
    .uk[$m] == .policyengine[$v]
  ' "${versions_body}" >/dev/null ||
    identity_failure "policyengine-uk ${served_uk_model} does not route to policyengine ${expected_policyengine_version}'s app"

  if [ -n "${expected_uk_dataset_sha256}" ]; then
    served_dataset="$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.dataset_sha256 // ""' "${versions_body}")"
    [ "${served_dataset}" = "${expected_uk_dataset_sha256}" ] ||
      identity_failure "the UK dataset SHA-256 served for policyengine ${expected_policyengine_version} is ${served_dataset:-none}, expected ${expected_uk_dataset_sha256}"
  fi

  if [ "${expected_latest}" = "true" ]; then
    jq -e --arg v "${expected_policyengine_version}" --arg m "${served_uk_model}" '
      .policyengine.latest == $v and .uk.latest == $m
    ' "${versions_body}" >/dev/null ||
      identity_failure "latest routes to policyengine $(jq -r '.policyengine.latest // "none"' "${versions_body}") and policyengine-uk $(jq -r '.uk.latest // "none"' "${versions_body}"), not ${expected_policyengine_version} and ${served_uk_model}"
  fi

  printf '%s serves policyengine %s, policyengine-uk %s, UK dataset %s%s\n' \
    "${base_url}" "${expected_policyengine_version}" "${served_uk_model}" \
    "$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.dataset_sha256 // "unrecorded"' "${versions_body}")" \
    "$([ "${expected_latest}" = "true" ] && printf ' as latest')"
elif [ -n "${expected_uk_model_version}${expected_uk_dataset_sha256}${expected_latest}" ]; then
  identity_failure "UK identity expectations need EXPECTED_POLICYENGINE_VERSION"
fi
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
