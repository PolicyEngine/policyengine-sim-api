#!/bin/bash
# Create a small economy report through a deployed Simulation Entrypoint, and
# require its run to succeed: current law against current law for Wales in
# 2026, which needs no stored policy.
# Usage: simulation-staging-economy-check.sh <base-url>
# Required env vars: the test client's SIMULATION_TEST_AUTH_ISSUER,
# SIMULATION_TEST_AUTH_AUDIENCE, SIMULATION_TEST_AUTH_CLIENT_ID and
# SIMULATION_TEST_AUTH_CLIENT_SECRET.
# Optional: ECONOMY_CHECK_DEADLINE_SECONDS (1800) and
# ECONOMY_CHECK_POLL_SECONDS (15).
# The access token is sent from a file and the client secret on standard
# input, so neither appears on a command line. Each request is given
# REQUEST_TIMEOUT_SECONDS; a read that gets no answer is asked again, as a
# 503 is, until the deadline.

set -euo pipefail

fail() {
  printf 'Staging economy check failed: %s\n' "$1" >&2
  exit 1
}

base_url="${1:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
deadline_seconds="${ECONOMY_CHECK_DEADLINE_SECONDS:-1800}"
poll_seconds="${ECONOMY_CHECK_POLL_SECONDS:-15}"
request_timeout_seconds=30
uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

for variable_name in SIMULATION_TEST_AUTH_ISSUER SIMULATION_TEST_AUTH_AUDIENCE \
  SIMULATION_TEST_AUTH_CLIENT_ID SIMULATION_TEST_AUTH_CLIENT_SECRET; do
  [[ -n "${!variable_name:-}" ]] || fail "${variable_name} must be set"
done
[[ "${deadline_seconds}" =~ ^[0-9]+$ ]] \
  || fail "ECONOMY_CHECK_DEADLINE_SECONDS must be a whole number of seconds"
[[ "${poll_seconds}" =~ ^[0-9]+$ ]] \
  || fail "ECONOMY_CHECK_POLL_SECONDS must be a whole number of seconds"

work_directory="$(mktemp -d)"
trap 'rm -rf "${work_directory}"' EXIT
chmod 700 "${work_directory}"

# Send one request; write its body to <name>.json and print its HTTP status.
# It fails, printing nothing, when no answer arrives.
send() {
  local name="$1"
  shift
  curl --silent --show-error --max-time "${request_timeout_seconds}" \
    --output "${work_directory}/${name}.json" --write-out '%{http_code}' "$@"
}

token_status="$(
  jq -n \
    --arg client_id "${SIMULATION_TEST_AUTH_CLIENT_ID}" \
    --arg client_secret "${SIMULATION_TEST_AUTH_CLIENT_SECRET}" \
    --arg audience "${SIMULATION_TEST_AUTH_AUDIENCE}" \
    '{client_id: $client_id, client_secret: $client_secret, audience: $audience, grant_type: "client_credentials"}' \
    | send token --request POST --header 'Content-Type: application/json' \
      --data @- "${SIMULATION_TEST_AUTH_ISSUER%/}/oauth/token"
)" || fail "the token request got no answer"
[[ "${token_status}" == "200" ]] || fail "the token request answered ${token_status}"
access_token="$(jq -r '.access_token // empty' "${work_directory}/token.json")"
[[ -n "${access_token}" ]] || fail "the token response has no access_token"
authorization_header="${work_directory}/authorization-header"
printf 'Authorization: Bearer %s\n' "${access_token}" > "${authorization_header}"

submission_status="$(
  jq -n '{
      country: "uk",
      report_type: "economy",
      label: "Staging economy check",
      year: 2026,
      region_code: "country/wales",
      baseline: {policy_id: null, dynamic_id: null},
      reform: {policy_id: null, dynamic_id: null},
      include_cliffs: false
    }' \
    | send submission --request POST --header "@${authorization_header}" \
      --header 'Content-Type: application/json' --data @- \
      "${base_url}/reports"
)" || fail "POST /reports got no answer; it may have created a report"
[[ "${submission_status}" == "201" ]] \
  || fail "POST /reports answered ${submission_status}: $(cat "${work_directory}/submission.json")"
submission="${work_directory}/submission.json"
report_id="$(jq -r '.report_id // empty' "${submission}")"
report_run_id="$(jq -r '.report_run_id // empty' "${submission}")"
[[ "${report_id}" =~ ${uuid_pattern} ]] || fail "the submission named no report"
[[ "${report_run_id}" =~ ${uuid_pattern} ]] || fail "the submission named no report run"
printf 'Created report %s, run %s (baseline reused: %s, reform reused: %s)\n' \
  "${report_id}" "${report_run_id}" \
  "$(jq -r '.baseline_reused' "${submission}")" "$(jq -r '.reform_reused' "${submission}")"

started_at="${SECONDS}"
while true; do
  report_body="${work_directory}/report.json"
  if ! read_status="$(
    send report --header "@${authorization_header}" "${base_url}/reports/${report_id}"
  )"; then
    printf 'GET /reports/%s got no answer; asking again\n' "${report_id}" >&2
  elif [[ "${read_status}" == "200" ]]; then
    newest_run_id="$(jq -r '.run.run_id // empty' "${report_body}")"
    [[ "${newest_run_id}" == "${report_run_id}" ]] \
      || fail "report ${report_id}'s newest run is ${newest_run_id:-missing}, not the created run ${report_run_id}"
    run_status="$(jq -r '.run.status' "${report_body}")"
    case "${run_status}" in
      succeeded)
        printf 'Report run %s succeeded on .py %s with %s\n' "${report_run_id}" \
          "$(jq -r '.run.policyengine_version' "${report_body}")" \
          "$(jq -r '.run.country_package_version' "${report_body}")"
        exit 0
        ;;
      failed)
        fail "report run ${report_run_id} failed: $(jq -r '.run.error_message // "no error message"' "${report_body}")"
        ;;
      pending | running) ;;
      *) fail "report run ${report_run_id} has an unknown status: ${run_status}" ;;
    esac
  elif [[ "${read_status}" != "503" ]]; then
    fail "GET /reports/${report_id} answered ${read_status}: $(cat "${report_body}")"
  fi
  ((SECONDS - started_at < deadline_seconds)) \
    || fail "report run ${report_run_id} did not finish within ${deadline_seconds} seconds"
  sleep "${poll_seconds}"
done
