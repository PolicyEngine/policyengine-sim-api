#!/usr/bin/env bash
# Point the safety-check Cloud Run job at the entrypoint revision that serves
# traffic, and keep its Cloud Scheduler entry.
#
# The job runs `python -m policyengine_simulation_entry.safety_check`: one
# recording pass per execution. It is updated only from a revision that was
# promoted (or restored by a rollback), so a rejected candidate never records
# anything. It copies the revision's image digest, service account, Modal
# token and database URL (at the secret versions the revision pins) and its
# plain telemetry settings; nothing else. A revision without the simulation
# database URL predates the safety check, so its schedule is paused.
#
# Environment: PROJECT_ID, REGION, SERVICE, SAFETY_CHECK_REVISION,
# SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT, RUNNER_TEMP.
set -euo pipefail

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

# Every five minutes: SAFETY_CHECK_INTERVAL in
# policyengine_simulation_entry.run_recording.
SCHEDULE="*/5 * * * *"
SECRETS=(SIMULATION_DATABASE_URL MODAL_TOKEN_ID MODAL_TOKEN_SECRET)
PLAIN_VARIABLES_FILTER='^(APP_ENVIRONMENT|CLOUD_RUN_REGION|OBSERVABILITY_[A-Z_]+|OTEL_[A-Z_]+|POLICYENGINE_OTEL_GOOGLE_AUDIENCE)$'

for variable_name in PROJECT_ID REGION SERVICE SAFETY_CHECK_REVISION \
  SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT RUNNER_TEMP; do
  [[ -n "${!variable_name:-}" ]] || fail "${variable_name} is required"
done
[[ "${PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || fail "PROJECT_ID is invalid"
[[ "${REGION}" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || fail "REGION is invalid"
[[ "${SERVICE}" =~ ^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$ ]] || fail "SERVICE is invalid"
[[ "${SAFETY_CHECK_REVISION}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || fail "SAFETY_CHECK_REVISION is invalid"
[[ "${SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com$ ]] \
  || fail "SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT is invalid"

job="${SERVICE}-safety-check"
location=(--project "${PROJECT_ID}" --region "${REGION}")
scheduler_location=(--project "${PROJECT_ID}" --location "${REGION}")

revision_json="$(gcloud run revisions describe "${SAFETY_CHECK_REVISION}" "${location[@]}" --format=json)"
revision_service="$(jq -r '.metadata.labels["serving.knative.dev/service"] // ""' <<<"${revision_json}")"
[[ "${revision_service}" == "${SERVICE}" ]] \
  || fail "${SAFETY_CHECK_REVISION} belongs to ${revision_service:-no service}, not ${SERVICE}"

if ! jq -e '.spec.containers[0].env // [] | any(.name == "SIMULATION_DATABASE_URL")' >/dev/null <<<"${revision_json}"; then
  if gcloud scheduler jobs describe "${job}" "${scheduler_location[@]}" >/dev/null 2>&1; then
    gcloud scheduler jobs pause "${job}" "${scheduler_location[@]}" --quiet
  fi
  printf '%s has no simulation database, so the safety check is paused.\n' "${SAFETY_CHECK_REVISION}"
  exit 0
fi

image="$(jq -er '.status.imageDigest // .spec.containers[0].image' <<<"${revision_json}")"
service_account="$(jq -er '.spec.serviceAccountName' <<<"${revision_json}")"

job_secrets=()
for secret_variable in "${SECRETS[@]}"; do
  reference="$(jq -er --arg name "${secret_variable}" '
    [.spec.containers[0].env[] | select(.name == $name) | .valueFrom.secretKeyRef
      | select(.name != null and .key != null) | "\(.name):\(.key)"]
    | if length == 1 then .[0] else error("not one secret reference") end
  ' <<<"${revision_json}")" || fail "${SAFETY_CHECK_REVISION} has no pinned secret for ${secret_variable}"
  job_secrets+=("${secret_variable}=${reference}")
done
job_secrets_csv="$(IFS=,; printf '%s' "${job_secrets[*]}")"

environment_file="${RUNNER_TEMP}/simulation-safety-check-environment.yaml"
trap 'rm -f "${environment_file}"' EXIT
jq --arg filter "${PLAIN_VARIABLES_FILTER}" '
  [.spec.containers[0].env[] | select(.value != null and (.name | test($filter)))
    | {(.name): .value}] | add // {}
' <<<"${revision_json}" > "${environment_file}"

gcloud run jobs deploy "${job}" "${location[@]}" \
  --image "${image}" \
  --service-account "${service_account}" \
  --set-secrets "${job_secrets_csv}" \
  --env-vars-file "${environment_file}" \
  --command /opt/venv/bin/python \
  --args=-m,policyengine_simulation_entry.safety_check \
  --tasks 1 \
  --max-retries 0 \
  --task-timeout 600s \
  --cpu 1 \
  --memory 512Mi \
  --labels "serving-revision=${SAFETY_CHECK_REVISION}" \
  --quiet

schedule_action=create
if gcloud scheduler jobs describe "${job}" "${scheduler_location[@]}" >/dev/null 2>&1; then
  schedule_action=update
fi
gcloud scheduler jobs "${schedule_action}" http "${job}" "${scheduler_location[@]}" \
  --schedule "${SCHEDULE}" \
  --time-zone Etc/UTC \
  --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job}:run" \
  --http-method POST \
  --oauth-service-account-email "${SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT}" \
  --attempt-deadline 60s \
  --quiet
if [[ "${schedule_action}" == "update" ]]; then
  gcloud scheduler jobs resume "${job}" "${scheduler_location[@]}" --quiet
fi
printf 'The safety check runs %s from %s.\n' "${image}" "${SAFETY_CHECK_REVISION}"
