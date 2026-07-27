#!/usr/bin/env bash

# Assign all stable Cloud Run service traffic to one exact, ready revision.
# The expected-current guard prevents this workflow from overwriting a traffic
# change made after its candidate deployment. The same command supports
# rollback by swapping the target and expected-current revisions.

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_ENTRYPOINT_GCP_PROJECT_ID:?SIMULATION_ENTRYPOINT_GCP_PROJECT_ID is required}"
region="${SIMULATION_ENTRYPOINT_GCP_REGION:-us-central1}"
service="${SIMULATION_ENTRYPOINT_SERVICE:?SIMULATION_ENTRYPOINT_SERVICE is required}"
target_revision="${SIMULATION_ENTRYPOINT_TARGET_REVISION:?SIMULATION_ENTRYPOINT_TARGET_REVISION is required}"
expected_current_revision="${SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION:?SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION is required}"

for revision in "${target_revision}" "${expected_current_revision}"; do
  if [ "${revision}" = "LATEST" ] || [ "${revision}" = "latest" ]; then
    printf 'Traffic revisions must be exact; LATEST is not allowed\n' >&2
    exit 2
  fi
done

active_revision() {
  jq -er '
    [
      .status.traffic[]?
      | select((.percent // 0) == 100)
      | .revisionName
      | select(type == "string" and length > 0)
    ]
    | if length == 1 then .[0]
      else error("service must have exactly one revision at 100 percent")
      end
  '
}

service_json="$("${gcloud_bin}" run services describe "${service}" \
  --project "${project_id}" \
  --region "${region}" \
  --format=json)"
current_revision="$(active_revision <<<"${service_json}")"

if [ "${current_revision}" != "${expected_current_revision}" ]; then
  printf 'Stable traffic changed after deployment: expected %s, found %s\n' \
    "${expected_current_revision}" "${current_revision}" >&2
  exit 2
fi

revision_json="$("${gcloud_bin}" run revisions describe "${target_revision}" \
  --project "${project_id}" \
  --region "${region}" \
  --format=json)"
revision_service="$(jq -r \
  '.metadata.labels["serving.knative.dev/service"] // empty' \
  <<<"${revision_json}")"

if [ "${revision_service}" != "${service}" ]; then
  printf 'Revision %s belongs to %s, not %s\n' \
    "${target_revision}" "${revision_service:-an unknown service}" "${service}" >&2
  exit 2
fi

if ! jq -e \
  '.status.conditions[]? | select(.type == "Ready" and .status == "True")' \
  >/dev/null <<<"${revision_json}"; then
  printf 'Revision %s is not Ready\n' "${target_revision}" >&2
  exit 2
fi

"${gcloud_bin}" run services update-traffic "${service}" \
  --project "${project_id}" \
  --region "${region}" \
  --to-revisions "${target_revision}=100"

updated_service_json="$("${gcloud_bin}" run services describe "${service}" \
  --project "${project_id}" \
  --region "${region}" \
  --format=json)"
updated_revision="$(active_revision <<<"${updated_service_json}")"

if [ "${updated_revision}" != "${target_revision}" ]; then
  printf 'Traffic update did not activate %s; found %s\n' \
    "${target_revision}" "${updated_revision}" >&2
  exit 2
fi
