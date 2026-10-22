#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
expected_revision="${2:-}"
# Stable hostnames can briefly continue serving the revision that was active
# immediately before a traffic update. Supplying that revision as argument 3
# enables bounded retries for only that documented transition state.
previous_revision="${3:-}"
# Release identity, checked against /versions, whose route maps hold versions
# as `latest` and app names under each version key. The expectations are all
# or nothing: EXPECTED_IDENTITY=required (existing-stack deployments) demands
# every one, and setting any one demands the rest, so a missing value can
# never silently weaken the check.
# - EXPECTED_POLICYENGINE_VERSION: that policyengine.py version has a route
#   and a recorded UK bundle identity for exactly that version;
# - EXPECTED_APP_NAME: the executor app just deployed for it; both that
#   version and its UK model must route to this app;
# - EXPECTED_UK_MODEL_VERSION / EXPECTED_UK_DATASET_SHA256: that bundle's UK
#   model and certified dataset SHA-256, or `unrecorded` for a release
#   published before digests were recorded (then /versions must report none);
# - EXPECTED_LATEST=true (optional): unpinned requests route to that version
#   and model.
# This is the identity the gateway reports from the routing registry; the
# numerical release sentinel checks what the worker computes.
# REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION=true (candidate checks only):
# /ready must report the entrypoint's own database reachable and at the
# migration revision the image expects, which shows that the runtime secret
# reached the revision and that the migration job ran. Stable and rollback
# checks leave it unset, because an older image may face a newer schema.
require_database_revision="${REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION:-}"
expected_identity="${EXPECTED_IDENTITY:-none}"
expected_policyengine_version="${EXPECTED_POLICYENGINE_VERSION:-}"
expected_app_name="${EXPECTED_APP_NAME:-}"
expected_uk_model_version="${EXPECTED_UK_MODEL_VERSION:-}"
expected_uk_dataset_sha256="${EXPECTED_UK_DATASET_SHA256:-}"
expected_latest="${EXPECTED_LATEST:-}"

case "${expected_identity}" in
  required | none) ;;
  *)
    printf 'EXPECTED_IDENTITY must be required or none, not %s\n' "${expected_identity}" >&2
    exit 2
    ;;
esac
case "${require_database_revision}" in
  "" | true) ;;
  *)
    printf 'REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION must be true or empty, not %s\n' \
      "${require_database_revision}" >&2
    exit 2
    ;;
esac
case "${expected_latest}" in
  "" | true) ;;
  *)
    printf 'EXPECTED_LATEST must be true or empty, not %s\n' "${expected_latest}" >&2
    exit 2
    ;;
esac
check_identity=false
if [ "${expected_identity}" = "required" ] ||
  [ -n "${expected_policyengine_version}${expected_app_name}${expected_uk_model_version}${expected_uk_dataset_sha256}${expected_latest}" ]; then
  check_identity=true
  for name in EXPECTED_POLICYENGINE_VERSION EXPECTED_APP_NAME EXPECTED_UK_MODEL_VERSION EXPECTED_UK_DATASET_SHA256; do
    if [ -z "${!name:-}" ]; then
      printf '%s is required: release identity expectations are all or nothing\n' "${name}" >&2
      exit 2
    fi
  done
  if ! [[ "${expected_uk_dataset_sha256}" =~ ^([0-9a-f]{64}|unrecorded)$ ]]; then
    printf 'EXPECTED_UK_DATASET_SHA256 must be 64 lowercase hexadecimal characters or unrecorded\n' >&2
    exit 2
  fi
fi
routing_max_attempts="${SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS:-24}"
routing_retry_delay_seconds="${SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS:-5}"
# A cold candidate may not reach its database within /ready's three-second
# wait; an unreachable database is asked about again this many times.
database_ready_max_attempts="${SIMULATION_DATABASE_READY_MAX_ATTEMPTS:-6}"

if ! [[ "${routing_max_attempts}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS must be a positive integer\n' >&2
  exit 2
fi
if ! [[ "${routing_retry_delay_seconds}" =~ ^[0-9]+$ ]]; then
  printf 'SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS must be a non-negative integer\n' >&2
  exit 2
fi
if ! [[ "${database_ready_max_attempts}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'SIMULATION_DATABASE_READY_MAX_ATTEMPTS must be a positive integer\n' >&2
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

ready_body="$(curl --fail --silent --show-error "${base_url}/ready")"
jq -e '.status == "ready"' <<<"${ready_body}" >/dev/null
if [ "${require_database_revision}" = "true" ]; then
  database_attempt=1
  while [ "${database_attempt}" -lt "${database_ready_max_attempts}" ] &&
    jq -e '.simulation_database.reachable == false' <<<"${ready_body}" >/dev/null; do
    printf '%s: the simulation database was not reached; asking again (%s/%s)\n' \
      "${base_url}" "${database_attempt}" "${database_ready_max_attempts}" >&2
    sleep "${routing_retry_delay_seconds}"
    ready_body="$(curl --fail --silent --show-error "${base_url}/ready")"
    database_attempt=$((database_attempt + 1))
  done
fi
if [ "${require_database_revision}" = "true" ] &&
  ! jq -e '
    .simulation_database.reachable == true
    and (.simulation_database.revision | type == "string")
    and .simulation_database.revision == .simulation_database.expected_revision
  ' <<<"${ready_body}" >/dev/null; then
  printf '%s: the simulation database is not at the expected revision: %s\n' \
    "${base_url}" "$(jq -c '.simulation_database' <<<"${ready_body}")" >&2
  exit 1
fi
curl --fail --silent --show-error "${base_url}/versions" --output "${versions_body}"
jq -e 'type == "object"' "${versions_body}" >/dev/null

identity_failure() {
  printf '%s: %s\n' "${base_url}" "$1" >&2
  exit 1
}

if [ "${check_identity}" = "true" ]; then
  jq -e --arg v "${expected_policyengine_version}" '
    (.policyengine[$v] | type == "string" and length > 0)
    and .bundles[$v].uk.policyengine_version == $v
  ' "${versions_body}" >/dev/null ||
    identity_failure "/versions has no route and UK bundle identity for policyengine ${expected_policyengine_version}"

  served_app="$(jq -r --arg v "${expected_policyengine_version}" '.policyengine[$v] // ""' "${versions_body}")"
  [ "${served_app}" = "${expected_app_name}" ] ||
    identity_failure "policyengine ${expected_policyengine_version} routes to ${served_app:-no app}, expected ${expected_app_name}"

  served_uk_model="$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.model_version // ""' "${versions_body}")"
  [ "${served_uk_model}" = "${expected_uk_model_version}" ] ||
    identity_failure "policyengine ${expected_policyengine_version} records policyengine-uk ${served_uk_model:-none}, expected ${expected_uk_model_version}"
  served_uk_app="$(jq -r --arg m "${served_uk_model}" '.uk[$m] // ""' "${versions_body}")"
  [ "${served_uk_app}" = "${expected_app_name}" ] ||
    identity_failure "policyengine-uk ${served_uk_model} routes to ${served_uk_app:-no app}, expected ${expected_app_name}"

  served_dataset="$(jq -r --arg v "${expected_policyengine_version}" '.bundles[$v].uk.dataset_sha256 // ""' "${versions_body}")"
  [ "${served_dataset:-unrecorded}" = "${expected_uk_dataset_sha256}" ] ||
    identity_failure "the UK dataset SHA-256 served for policyengine ${expected_policyengine_version} is ${served_dataset:-none}, expected ${expected_uk_dataset_sha256}"

  if [ "${expected_latest}" = "true" ]; then
    jq -e --arg v "${expected_policyengine_version}" --arg m "${served_uk_model}" '
      .policyengine.latest == $v and .uk.latest == $m
    ' "${versions_body}" >/dev/null ||
      identity_failure "latest routes to policyengine $(jq -r '.policyengine.latest // "none"' "${versions_body}") and policyengine-uk $(jq -r '.uk.latest // "none"' "${versions_body}"), not ${expected_policyengine_version} and ${served_uk_model}"
  fi

  printf '%s serves policyengine %s from %s, policyengine-uk %s, UK dataset %s%s\n' \
    "${base_url}" "${expected_policyengine_version}" "${served_app}" \
    "${served_uk_model}" "${served_dataset:-unrecorded}" \
    "$([ "${expected_latest}" = "true" ] && printf ' as latest')"
fi
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
