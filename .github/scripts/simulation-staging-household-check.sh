#!/bin/bash
# Calculate the fixed staging household through a deployed Simulation
# Entrypoint, and require its household job to succeed.
# Usage: simulation-staging-household-check.sh <base-url>
# Required env vars: STAGING_HOUSEHOLD_ID (the ID of a US household stored in
# the staging Public API, from the beta variable
# SIMULATION_STAGING_HOUSEHOLD_ID), and the test client's
# SIMULATION_TEST_AUTH_ISSUER, SIMULATION_TEST_AUTH_AUDIENCE,
# SIMULATION_TEST_AUTH_CLIENT_ID and SIMULATION_TEST_AUTH_CLIENT_SECRET.
# Optional: HOUSEHOLD_CHECK_DEADLINE_SECONDS (600) and
# HOUSEHOLD_CHECK_POLL_SECONDS (5).
# The access token is sent from a file and the client secret on standard
# input, so neither appears on a command line.

set -euo pipefail

fail() {
  printf 'Staging household check failed: %s\n' "$1" >&2
  exit 1
}

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
household_id="${STAGING_HOUSEHOLD_ID:-}"
deadline_seconds="${HOUSEHOLD_CHECK_DEADLINE_SECONDS:-600}"
poll_seconds="${HOUSEHOLD_CHECK_POLL_SECONDS:-5}"
uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

[[ -n "${household_id}" ]] \
  || fail "STAGING_HOUSEHOLD_ID is empty: set the beta environment variable SIMULATION_STAGING_HOUSEHOLD_ID to the ID of a US household stored in the staging Public API"
[[ "${household_id}" =~ ${uuid_pattern} ]] \
  || fail "STAGING_HOUSEHOLD_ID is not a UUID in lowercase hexadecimal"
for variable_name in SIMULATION_TEST_AUTH_ISSUER SIMULATION_TEST_AUTH_AUDIENCE \
  SIMULATION_TEST_AUTH_CLIENT_ID SIMULATION_TEST_AUTH_CLIENT_SECRET; do
  [[ -n "${!variable_name:-}" ]] || fail "${variable_name} must be set"
done
[[ "${deadline_seconds}" =~ ^[0-9]+$ ]] \
  || fail "HOUSEHOLD_CHECK_DEADLINE_SECONDS must be a whole number of seconds"
[[ "${poll_seconds}" =~ ^[0-9]+$ ]] \
  || fail "HOUSEHOLD_CHECK_POLL_SECONDS must be a whole number of seconds"

work_directory="$(mktemp -d)"
trap 'rm -rf "${work_directory}"' EXIT
chmod 700 "${work_directory}"

# Send one request; write its body to <name>.json and print its HTTP status.
send() {
  local name="$1"
  shift
  curl --silent --show-error --output "${work_directory}/${name}.json" \
    --write-out '%{http_code}' "$@"
}

token_status="$(
  jq -n \
    --arg client_id "${SIMULATION_TEST_AUTH_CLIENT_ID}" \
    --arg client_secret "${SIMULATION_TEST_AUTH_CLIENT_SECRET}" \
    --arg audience "${SIMULATION_TEST_AUTH_AUDIENCE}" \
    '{client_id: $client_id, client_secret: $client_secret, audience: $audience, grant_type: "client_credentials"}' \
    | send token --request POST --header 'Content-Type: application/json' \
      --data @- "${SIMULATION_TEST_AUTH_ISSUER%/}/oauth/token"
)"
[[ "${token_status}" == "200" ]] || fail "the token request answered ${token_status}"
access_token="$(jq -r '.access_token // empty' "${work_directory}/token.json")"
[[ -n "${access_token}" ]] || fail "the token response has no access_token"
authorization_header="${work_directory}/authorization-header"
printf 'Authorization: Bearer %s\n' "${access_token}" > "${authorization_header}"

submission_status="$(
  jq -n --arg household_id "${household_id}" \
    '{country: "us", household_id: $household_id}' \
    | send submission --request POST --header "@${authorization_header}" \
      --header 'Content-Type: application/json' --data @- \
      "${base_url}/household-calculations"
)"
[[ "${submission_status}" == "202" ]] \
  || fail "POST /household-calculations answered ${submission_status}: $(cat "${work_directory}/submission.json")"
job_id="$(jq -r '.job_id // empty' "${work_directory}/submission.json")"
[[ "${job_id}" =~ ${uuid_pattern} ]] || fail "the submission named no household job"
printf 'Submitted household job %s for staging household %s\n' "${job_id}" "${household_id}"

started_at="${SECONDS}"
while true; do
  job_status_code="$(
    send job --header "@${authorization_header}" \
      "${base_url}/household-calculations/${job_id}"
  )"
  job_body="${work_directory}/job.json"
  if [[ "${job_status_code}" == "200" ]]; then
    job_status="$(jq -r '.status' "${job_body}")"
    case "${job_status}" in
      succeeded)
        [[ "$(jq -r '.result.status // empty' "${job_body}")" == "succeeded" ]] \
          || fail "household job ${job_id} succeeded without a succeeded result document"
        printf 'Household job %s succeeded on .py %s with %s\n' "${job_id}" \
          "$(jq -r '.policyengine_version' "${job_body}")" \
          "$(jq -r '.country_package_version' "${job_body}")"
        exit 0
        ;;
      failed)
        fail "household job ${job_id} failed: $(jq -r '.error_message // "no error message"' "${job_body}")"
        ;;
      pending | running) ;;
      *) fail "household job ${job_id} has an unknown status: ${job_status}" ;;
    esac
  elif [[ "${job_status_code}" != "503" ]]; then
    fail "GET /household-calculations/${job_id} answered ${job_status_code}: $(cat "${job_body}")"
  fi
  ((SECONDS - started_at < deadline_seconds)) \
    || fail "household job ${job_id} did not finish within ${deadline_seconds} seconds"
  sleep "${poll_seconds}"
done
