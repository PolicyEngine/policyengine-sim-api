#!/bin/bash
# Stop the Modal container running a staging calculation, as a pre-emption
# does, and require the calculation to succeed anyway.
# Usage: simulation-staging-preemption-check.sh <household|report> <base-url>
# Run from projects/policyengine-simulation-executor, whose environment has
# the Modal client.
#
# household: calculates the stored staging household through the deployed
#   Simulation Entrypoint, and stops the household function's container once
#   the job is running, that is once the function has claimed it.
# report: creates the staging economy check's report, current law against
#   current law for Wales in 2026, whose simulation runs that check has just
#   left to reuse, and stops the coordinator's container once the report run
#   is running, that is once the coordinator has claimed it.
#
# The container is the one Modal's call graph names for the Modal call the
# record names (modal_call_id), and it must be listed in Modal's staging
# environment. `modal container stop` sends it SIGINT, and Modal gives the
# call's input to another container under the same call ID, as it does
# after a pre-emption. The check passes once the record succeeds under that
# call, completed at least 5 seconds after the stop (both counted in whole
# seconds), and the call graph shows the call's input finished in another
# container. A record completed sooner proves nothing: the stopped container
# may have written the success before the signal reached it, and Modal's
# rerun then finds the record finished and only returns its summary. A
# rerun in a new container can't finish within 5 seconds of the stop.
# A calculation that finishes before its container is stopped proves
# nothing, so a new one is submitted, up to PREEMPTION_CHECK_ATTEMPTS
# times, once Modal no longer lists any container the last attempt's call
# ran in: on such a warm container the calculation would finish too fast
# to stop. A record
# that fails, or doesn't finish within the deadline, fails the check at
# once. A stop that fails, as Modal's does for a container that has ended,
# is not tried again: the check follows the record to its end, so a record
# left running by a container that ended for another reason, such as
# running out of memory, fails at its deadline rather than being submitted
# again. Every Modal command may take 60 seconds.
#
# Required env vars: the test client's SIMULATION_TEST_AUTH_ISSUER,
# SIMULATION_TEST_AUTH_AUDIENCE, SIMULATION_TEST_AUTH_CLIENT_ID and
# SIMULATION_TEST_AUTH_CLIENT_SECRET; MODAL_TOKEN_ID and MODAL_TOKEN_SECRET,
# which the Modal client reads; MODAL_ENVIRONMENT, which must be staging;
# and, for household, STAGING_HOUSEHOLD_ID (the ID of a US household stored
# in the staging Public API).
# Optional: PREEMPTION_CHECK_ATTEMPTS (3); PREEMPTION_CHECK_DEADLINE_SECONDS,
# each attempt's deadline (600 for household, 900 for report);
# PREEMPTION_CHECK_POLL_SECONDS (2); PREEMPTION_CHECK_RERUN_SECONDS (120),
# how long after the record succeeds the call graph may take to show the
# call's other container; and PREEMPTION_CHECK_SCALEDOWN_SECONDS (180), how
# long to wait for the last attempt's container to leave before a new
# attempt (Modal stops an idle container after 60 seconds by default).
# The access token is sent from a file, and the client secret on standard
# input through jq's environment, so neither appears on any command line. A submission, which isn't
# asked again, may take 60 seconds, Cloud Run's own limit; every other
# request 30. A read that gets no answer, a 429 or any 5xx is asked again
# until the deadline.

set -euo pipefail

fail() {
  printf 'Staging preemption check failed: %s\n' "$1" >&2
  exit 1
}

kind="${1:-}"
base_url="${2:?Simulation Entrypoint base URL is required}"
base_url="${base_url%/}"
case "${kind}" in
  household) default_deadline_seconds=600 ;;
  report) default_deadline_seconds=900 ;;
  *) fail "the first argument names the calculation, household or report, not '${kind}'" ;;
esac
attempts="${PREEMPTION_CHECK_ATTEMPTS:-3}"
deadline_seconds="${PREEMPTION_CHECK_DEADLINE_SECONDS:-${default_deadline_seconds}}"
poll_seconds="${PREEMPTION_CHECK_POLL_SECONDS:-2}"
rerun_seconds="${PREEMPTION_CHECK_RERUN_SECONDS:-120}"
scaledown_seconds="${PREEMPTION_CHECK_SCALEDOWN_SECONDS:-180}"
request_timeout_seconds=30
submission_timeout_seconds=60
modal_command_timeout_seconds=60
# A record completed sooner after the stop may have been completed by the
# stopped container, not by Modal's rerun in a new one.
rerun_minimum_seconds=5
uuid_pattern='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
call_id_pattern='^fc-[0-9A-Za-z]+$'
container_id_pattern='^ta-[0-9A-Za-z]+$'

required_variables=(SIMULATION_TEST_AUTH_ISSUER SIMULATION_TEST_AUTH_AUDIENCE
  SIMULATION_TEST_AUTH_CLIENT_ID SIMULATION_TEST_AUTH_CLIENT_SECRET
  MODAL_TOKEN_ID MODAL_TOKEN_SECRET MODAL_ENVIRONMENT)
if [[ "${kind}" == "household" ]]; then
  required_variables+=(STAGING_HOUSEHOLD_ID)
fi
for variable_name in "${required_variables[@]}"; do
  [[ -n "${!variable_name:-}" ]] || fail "${variable_name} must be set"
done
[[ "${MODAL_ENVIRONMENT}" == "staging" ]] \
  || fail "MODAL_ENVIRONMENT is ${MODAL_ENVIRONMENT}: this check stops containers, so it runs only in Modal's staging environment"
if [[ "${kind}" == "household" ]]; then
  [[ "${STAGING_HOUSEHOLD_ID}" =~ ${uuid_pattern} ]] \
    || fail "STAGING_HOUSEHOLD_ID is not a UUID in lowercase hexadecimal"
fi
[[ "${attempts}" =~ ^[1-9][0-9]*$ ]] \
  || fail "PREEMPTION_CHECK_ATTEMPTS must be a whole number of at least 1"
for setting in "PREEMPTION_CHECK_DEADLINE_SECONDS=${deadline_seconds}" \
  "PREEMPTION_CHECK_POLL_SECONDS=${poll_seconds}" \
  "PREEMPTION_CHECK_RERUN_SECONDS=${rerun_seconds}" \
  "PREEMPTION_CHECK_SCALEDOWN_SECONDS=${scaledown_seconds}"; do
  [[ "${setting#*=}" =~ ^[0-9]+$ ]] \
    || fail "${setting%%=*} must be a whole number of seconds"
done

work_directory="$(mktemp -d)"
trap 'rm -rf "${work_directory}"' EXIT
chmod 700 "${work_directory}"

# Send one request, allowing it <seconds>; write its body to <name>.json and
# print its HTTP status. It fails when no answer arrives.
send() {
  local name="$1"
  local seconds="$2"
  shift 2
  curl --silent --show-error --max-time "${seconds}" \
    --output "${work_directory}/${name}.json" --write-out '%{http_code}' "$@"
}

token_status="$(
  jq -n '{
      client_id: env.SIMULATION_TEST_AUTH_CLIENT_ID,
      client_secret: env.SIMULATION_TEST_AUTH_CLIENT_SECRET,
      audience: env.SIMULATION_TEST_AUTH_AUDIENCE,
      grant_type: "client_credentials"
    }' \
    | send token "${request_timeout_seconds}" --request POST --header 'Content-Type: application/json' \
      --data @- "${SIMULATION_TEST_AUTH_ISSUER%/}/oauth/token"
)" || fail "the token request got no answer"
[[ "${token_status}" == "200" ]] || fail "the token request answered ${token_status}"
access_token="$(jq -r '.access_token // empty' "${work_directory}/token.json")"
[[ -n "${access_token}" ]] || fail "the token response has no access_token"
authorization_header="${work_directory}/authorization-header"
printf 'Authorization: Bearer %s\n' "${access_token}" > "${authorization_header}"

# Run a command in the executor's environment, which has the Modal client,
# allowing it modal_command_timeout_seconds.
modal_command() {
  timeout "${modal_command_timeout_seconds}" uv run --frozen "$@"
}

# Print the inputs of Modal call <call-id> as JSON; fail when Modal can't be
# asked (src/modal/utils/describe_call_inputs.py).
call_inputs() {
  modal_command python -m src.modal.utils.describe_call_inputs "$1"
}

# Of the containers <container-id>..., print those Modal lists as running
# in MODAL_ENVIRONMENT, one per line. Return 0 when it lists any, 1 when it
# lists none, and 2 when Modal couldn't list the environment's containers
# readably, which says nothing about them.
containers_listed() {
  local listing listed
  if ! listing="$(modal_command modal container list --env "${MODAL_ENVIRONMENT}" --json)"; then
    printf 'Modal could not list the containers in environment %s; asking again\n' \
      "${MODAL_ENVIRONMENT}" >&2
    return 2
  fi
  if ! listed="$(
    jq -r '[.[] | .["Container ID"]] as $running
      | $ARGS.positional[] | select(. as $id | $running | index($id))' \
      --args "$@" <<<"${listing}" 2>/dev/null
  )"; then
    printf 'Modal could not list the containers in environment %s readably; asking again\n' \
      "${MODAL_ENVIRONMENT}" >&2
    return 2
  fi
  [[ -n "${listed}" ]] || return 1
  printf '%s\n' "${listed}"
}

# Create the attempt's record. Sets record_url, record_noun, record_label
# and, for a report, report_run_id.
submit() {
  local submission="${work_directory}/submission.json"
  local submission_status
  if [[ "${kind}" == "household" ]]; then
    submission_status="$(
      jq -n --arg household_id "${STAGING_HOUSEHOLD_ID}" \
        '{country: "us", household_id: $household_id}' \
        | send submission "${submission_timeout_seconds}" --request POST \
          --header "@${authorization_header}" --header 'Content-Type: application/json' \
          --data @- "${base_url}/household-calculations"
    )" || fail "POST /household-calculations got no answer; it may have created a household job"
    [[ "${submission_status}" == "202" ]] \
      || fail "POST /household-calculations answered ${submission_status}: $(cat "${submission}")"
    local job_id
    job_id="$(jq -r '.job_id // empty' "${submission}")"
    [[ "${job_id}" =~ ${uuid_pattern} ]] || fail "the submission named no household job"
    record_url="${base_url}/household-calculations/${job_id}"
    record_noun="household job"
    record_label="${record_noun} ${job_id}"
    printf 'Submitted household job %s for staging household %s\n' \
      "${job_id}" "${STAGING_HOUSEHOLD_ID}"
  else
    submission_status="$(
      jq -n '{
          country: "uk",
          report_type: "economy",
          label: "Staging preemption check",
          year: 2026,
          region_code: "country/wales",
          baseline: {policy_id: null, dynamic_id: null},
          reform: {policy_id: null, dynamic_id: null},
          include_cliffs: false
        }' \
        | send submission "${submission_timeout_seconds}" --request POST \
          --header "@${authorization_header}" --header 'Content-Type: application/json' \
          --data @- "${base_url}/reports"
    )" || fail "POST /reports got no answer; it may have created a report"
    [[ "${submission_status}" == "201" ]] \
      || fail "POST /reports answered ${submission_status}: $(cat "${submission}")"
    local report_id
    report_id="$(jq -r '.report_id // empty' "${submission}")"
    report_run_id="$(jq -r '.report_run_id // empty' "${submission}")"
    [[ "${report_id}" =~ ${uuid_pattern} ]] || fail "the submission named no report"
    [[ "${report_run_id}" =~ ${uuid_pattern} ]] || fail "the submission named no report run"
    record_url="${base_url}/reports/${report_id}"
    record_noun="report run"
    record_label="${record_noun} ${report_run_id}"
    printf 'Created report %s, run %s (baseline reused: %s, reform reused: %s)\n' \
      "${report_id}" "${report_run_id}" \
      "$(jq -r '.baseline_reused' "${submission}")" "$(jq -r '.reform_reused' "${submission}")"
  fi
}

# Read the record once. Sets record_status, record_call_id, record_error,
# record_completed_at and last_read; leaves record_status empty when the
# service can't answer now.
read_record() {
  local record="${work_directory}/record.json"
  local read_status
  record_status=""
  if ! read_status="$(
    send record "${request_timeout_seconds}" --header "@${authorization_header}" "${record_url}"
  )"; then
    last_read="it got no answer"
    printf 'GET %s got no answer; asking again\n' "${record_url}" >&2
    return 0
  fi
  if [[ "${read_status}" == "429" || "${read_status}" == 5?? ]]; then
    # The service, or Cloud Run's front end in its place, can't answer now.
    last_read="it answered ${read_status}"
    printf 'GET %s answered %s; asking again\n' "${record_url}" "${read_status}" >&2
    return 0
  fi
  [[ "${read_status}" == "200" ]] \
    || fail "GET ${record_url} answered ${read_status}: $(cat "${record}")"
  # The household job's fields, or the report's newest run's.
  local record_filter=''
  if [[ "${kind}" == "report" ]]; then
    local newest_run_id
    newest_run_id="$(jq -r '.run.run_id // empty' "${record}")"
    [[ "${newest_run_id}" == "${report_run_id}" ]] \
      || fail "the report's newest run is ${newest_run_id:-missing}, not the created run ${report_run_id}"
    record_filter='.run'
  fi
  record_status="$(jq -r "${record_filter}.status" "${record}")"
  record_call_id="$(jq -r "${record_filter}.modal_call_id // empty" "${record}")"
  record_error="$(jq -r "${record_filter}.error_message // \"no error message\"" "${record}")"
  record_completed_at="$(jq -r "${record_filter}.completed_at // empty" "${record}")"
  if [[ -n "${record_call_id}" && ! "${record_call_id}" =~ ${call_id_pattern} ]]; then
    fail "${record_label} names Modal call '${record_call_id}', which is not a Modal call ID"
  fi
  if [[ "${kind}" == "household" && "${record_status}" == "succeeded" ]]; then
    [[ "$(jq -r '.result.status // empty' "${record}")" == "succeeded" ]] \
      || fail "${record_label} succeeded without a succeeded result document"
  fi
  last_read="the ${record_noun} was ${record_status}"
  return 0
}

# Find the container running the record's call. Sets container_id once the
# call graph names a running input's container that Modal lists in
# MODAL_ENVIRONMENT; until then, unlisted_note may say why not.
find_container() {
  local inputs candidate
  if ! inputs="$(call_inputs "${record_call_id}")"; then
    printf 'Modal could not say which container runs call %s yet; asking again\n' \
      "${record_call_id}" >&2
    return 0
  fi
  candidate="$(
    jq -r '[.[] | select(.status == "PENDING" and .container_id != "")] | first | .container_id // empty' \
      <<<"${inputs}"
  )" || fail "the call-input helper printed something other than a list of inputs for call ${record_call_id}"
  [[ -n "${candidate}" ]] || return 0
  [[ "${candidate}" =~ ${container_id_pattern} ]] \
    || fail "Modal names '${candidate}' as call ${record_call_id}'s container, which is not a Modal container ID"
  local listed_status=0
  containers_listed "${candidate}" >/dev/null || listed_status=$?
  case "${listed_status}" in
    0)
      container_id="${candidate}"
      printf 'Call %s of %s runs in container %s\n' "${record_call_id}" "${record_label}" "${container_id}"
      ;;
    1)
      unlisted_note="container ${candidate} is not listed in Modal environment ${MODAL_ENVIRONMENT}"
      printf '%s; asking again\n' "${unlisted_note}" >&2
      ;;
    # Modal couldn't list: the next read asks again.
    *) listing_note="Modal could not list the containers in environment ${MODAL_ENVIRONMENT}" ;;
  esac
}

# Set leaving_containers to the container the check found or stopped and
# every container the call graph says ran an input of this attempt's call,
# such as the one Modal's rerun ran in, so the next attempt can wait for
# them to leave: a warm container may finish a calculation before the check
# sees it running.
remember_call_containers() {
  local call="${stopped_call_id:-${record_call_id}}"
  local inputs candidate
  leaving_containers=()
  if [[ -n "${container_id}" ]]; then
    leaving_containers+=("${container_id}")
  fi
  [[ -n "${call}" ]] || return 0
  inputs="$(call_inputs "${call}")" || return 0
  while IFS= read -r candidate; do
    if [[ "${candidate}" =~ ${container_id_pattern} \
      && " ${leaving_containers[*]+${leaving_containers[*]}} " != *" ${candidate} "* ]]; then
      leaving_containers+=("${candidate}")
    fi
  done < <(jq -r '.[] | select(.container_id != "") | .container_id' <<<"${inputs}" 2>/dev/null || true)
}

# Return 0 when the record completed after its container was stopped, so
# its success comes from the rerun; otherwise set attempt_outcome to missed.
completed_after_the_stop() {
  local completed_epoch
  [[ -n "${record_completed_at}" ]] \
    || fail "${record_label} succeeded with no completion time"
  completed_epoch="$(
    jq -rn --arg completed_at "${record_completed_at}" \
      '$completed_at | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601'
  )" || fail "${record_label}'s completion time ${record_completed_at} is not a UTC time"
  if ((completed_epoch < stopped_at_epoch)); then
    attempt_outcome="missed"
    miss_reason="${record_label} completed at ${record_completed_at}, before its container ${stopped_container_id} was stopped, so Modal's rerun of call ${stopped_call_id} found it finished"
    return 1
  fi
  if ((completed_epoch - stopped_at_epoch < rerun_minimum_seconds)); then
    attempt_outcome="missed"
    miss_reason="${record_label} completed at ${record_completed_at}, $((completed_epoch - stopped_at_epoch)) seconds after its container ${stopped_container_id} was stopped: too soon for Modal to have run call ${stopped_call_id} again in a new container, so the stopped container may have finished it before the signal reached it"
    return 1
  fi
  return 0
}

# Wait until Modal lists none of leaving_containers, at most
# scaledown_seconds, so the next attempt's calculation starts in a new
# container rather than a warm one the last attempt left.
wait_for_containers_to_leave() {
  local waiting_since="${SECONDS}"
  local leaving listed_status still_listed
  ((${#leaving_containers[@]} > 0)) || return 0
  for leaving in "${leaving_containers[@]}"; do
    printf 'Before the next attempt, waiting for container %s to leave Modal environment %s\n' \
      "${leaving}" "${MODAL_ENVIRONMENT}"
  done
  while true; do
    listed_status=0
    still_listed="$(containers_listed "${leaving_containers[@]}")" || listed_status=$?
    ((listed_status != 1)) || return 0
    if ((SECONDS - waiting_since >= scaledown_seconds)); then
      if ((listed_status == 0)); then
        while IFS= read -r leaving; do
          printf 'container %s is still listed after %d seconds\n' "${leaving}" "${scaledown_seconds}" >&2
        done <<<"${still_listed}"
      else
        printf 'Modal could not say within %d seconds whether the last attempt'"'"'s containers have left\n' \
          "${scaledown_seconds}" >&2
      fi
      printf 'Submitting again anyway\n' >&2
      return 0
    fi
    sleep "${poll_seconds}"
  done
}

# After the record succeeded: require the call graph to show the stopped
# call's input finished in another container. Sets attempt_outcome.
confirm_rerun() {
  local waiting_since="${SECONDS}"
  local inputs rerun_container_id=""
  while true; do
    if inputs="$(call_inputs "${stopped_call_id}")"; then
      rerun_container_id="$(
        jq -r --arg stopped "${stopped_container_id}" \
          '[.[] | select(.status == "SUCCESS" and .container_id != "" and .container_id != $stopped)] | first | .container_id // empty' \
          <<<"${inputs}"
      )" || fail "the call-input helper printed something other than a list of inputs for call ${stopped_call_id}"
    fi
    if [[ -n "${rerun_container_id}" ]]; then
      printf '%s succeeded after its container %s was stopped: Modal ran call %s again in container %s\n' \
        "${record_label}" "${stopped_container_id}" "${stopped_call_id}" "${rerun_container_id}"
      attempt_outcome="proven"
      return 0
    fi
    if ((SECONDS - waiting_since >= rerun_seconds)); then
      attempt_outcome="missed"
      miss_reason="${record_label} succeeded, but Modal's call graph never showed call ${stopped_call_id} finishing in a container other than the stopped ${stopped_container_id} within ${rerun_seconds} seconds, so it may have finished before the stop"
      return 0
    fi
    sleep "${poll_seconds}"
  done
}

# Submit one calculation, stop its container once it's running, and follow
# it to the end. Sets attempt_outcome to proven or missed (with
# miss_reason); fails the check for a failed or late calculation.
attempt_once() {
  submit
  record_call_id=""
  container_id=""
  stopped_container_id=""
  stopped_call_id=""
  stopped_at_epoch=0
  unlisted_note=""
  listing_note=""
  stop_failure=""
  last_read="nothing yet"
  local started_at="${SECONDS}"
  while true; do
    read_record
    # A read the service answered (record_status set) must name the stopped
    # call: Modal runs it again under the same ID.
    if [[ -n "${record_status}" && -n "${stopped_call_id}" \
      && "${record_call_id}" != "${stopped_call_id}" ]]; then
      fail "${record_label} names ${record_call_id:+call }${record_call_id:-no call} after its container ${stopped_container_id} was stopped, not call ${stopped_call_id}, which Modal runs again under the same ID"
    fi
    case "${record_status}" in
      "") ;;
      succeeded)
        if [[ -n "${stop_failure}" ]]; then
          attempt_outcome="missed"
          miss_reason="${record_label} finished after ${stop_failure}"
        elif [[ -z "${stopped_container_id}" ]]; then
          attempt_outcome="missed"
          miss_reason="${record_label} finished before its container was stopped${unlisted_note:+ (${unlisted_note})}${listing_note:+ (${listing_note})}"
        elif completed_after_the_stop; then
          confirm_rerun
        fi
        return 0
        ;;
      failed)
        fail "${record_label} failed${stopped_container_id:+ after its container ${stopped_container_id} was stopped}${stop_failure:+ after ${stop_failure}}: ${record_error}"
        ;;
      pending | running)
        if [[ -z "${stopped_container_id}" && -z "${stop_failure}" && -n "${record_call_id}" ]]; then
          [[ -n "${container_id}" ]] || find_container
          if [[ "${record_status}" == "running" && -n "${container_id}" ]]; then
            local stop_status=0
            modal_command modal container stop --yes "${container_id}" || stop_status=$?
            if ((stop_status == 0)); then
              stopped_at_epoch="$(date -u +%s)"
              stopped_container_id="${container_id}"
              stopped_call_id="${record_call_id}"
              printf 'Stopped container %s while %s was running; waiting for Modal to run call %s again\n' \
                "${stopped_container_id}" "${record_label}" "${stopped_call_id}"
            else
              # Modal refuses to stop a container that has ended. The
              # check doesn't stop it again or submit another calculation
              # while this one may still be running.
              stop_failure="modal container stop failed for container ${container_id} (exit status ${stop_status}) while ${record_label} was running"
              printf '%s; following it to its end\n' "${stop_failure}" >&2
            fi
          fi
        fi
        ;;
      *) fail "${record_label} has an unknown status: ${record_status}" ;;
    esac
    ((SECONDS - started_at < deadline_seconds)) \
      || fail "${record_label} did not finish within ${deadline_seconds} seconds (last read: ${last_read})${stop_failure:+; ${stop_failure}}"
    sleep "${poll_seconds}"
  done
}

for ((attempt = 1; attempt <= attempts; attempt++)); do
  printf 'Attempt %d of %d\n' "${attempt}" "${attempts}"
  attempt_outcome=""
  miss_reason=""
  attempt_once
  [[ "${attempt_outcome}" != "proven" ]] || exit 0
  printf 'Attempt %d of %d proved nothing: %s\n' "${attempt}" "${attempts}" "${miss_reason}" >&2
  if ((attempt < attempts)); then
    remember_call_containers
    wait_for_containers_to_leave
  fi
done
fail "no attempt in ${attempts} showed a ${kind} calculation finishing in another container after its container was stopped; the last: ${miss_reason}"
