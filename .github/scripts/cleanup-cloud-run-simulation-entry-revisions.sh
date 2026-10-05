#!/usr/bin/env bash

# Remove every Cloud Run entrypoint revision except the exact revision that
# passed the deployment workflow and now receives all stable service traffic.

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_ENTRYPOINT_GCP_PROJECT_ID:?SIMULATION_ENTRYPOINT_GCP_PROJECT_ID is required}"
region="${SIMULATION_ENTRYPOINT_GCP_REGION:-us-central1}"
service="${SIMULATION_ENTRYPOINT_SERVICE:?SIMULATION_ENTRYPOINT_SERVICE is required}"
successful_revision="${SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION:?SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION is required}"

fail() {
  printf 'Cloud Run revision cleanup error: %s\n' "$1" >&2
  exit 1
}

[[ "${successful_revision}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || fail "SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION is invalid"

service_json="$(
  "${gcloud_bin}" run services describe "${service}" \
    --project "${project_id}" \
    --region "${region}" \
    --format=json
)"
revisions_json="$(
  "${gcloud_bin}" run revisions list \
    --service "${service}" \
    --project "${project_id}" \
    --region "${region}" \
    --format=json
)"

newest_revision="$(
  jq -er '
    max_by(.metadata.creationTimestamp)
    | .metadata.name
    | select(type == "string" and length > 0)
  ' <<<"${revisions_json}"
)" || fail "could not resolve the newest revision of ${service}"

[[ "${newest_revision}" == "${successful_revision}" ]] \
  || fail "${successful_revision} is not the newest revision of ${service}"

active_revision="$(
  jq -er '
    [
      .status.traffic[]?
      | select((.percent // 0) > 0)
      | {revision: .revisionName, percent}
    ]
    | if length == 1 and .[0].percent == 100
      then .[0].revision
      else error("service must have exactly one revision at 100 percent")
      end
  ' <<<"${service_json}"
)" || fail "${service} does not have exactly one revision at 100 percent"

[[ "${active_revision}" == "${successful_revision}" ]] \
  || fail "${successful_revision} is not the revision receiving stable traffic"

if ! jq -e --arg revision "${successful_revision}" '
  any(
    .[];
    .metadata.name == $revision
    and any(
      .status.conditions[]?;
      .type == "Ready" and .status == "True"
    )
  )
' >/dev/null <<<"${revisions_json}"; then
  fail "${successful_revision} is not a ready revision of ${service}"
fi

# A revision cannot be deleted while a traffic tag references it. Remove only
# tags that point to older revisions, preserving the successful candidate URL.
old_tags=()
while IFS= read -r tag; do
  [[ -n "${tag}" ]] && old_tags+=("${tag}")
done < <(
  jq -r --arg revision "${successful_revision}" '
    .status.traffic[]?
    | select(
        (.tag | type) == "string"
        and .revisionName != $revision
      )
    | .tag
  ' <<<"${service_json}"
)

if ((${#old_tags[@]} > 0)); then
  old_tags_csv="$(IFS=,; printf '%s' "${old_tags[*]}")"
  "${gcloud_bin}" run services update-traffic "${service}" \
    --project "${project_id}" \
    --region "${region}" \
    --remove-tags "${old_tags_csv}" \
    --quiet
fi

deleted_count=0
while IFS= read -r revision; do
  [[ -n "${revision}" ]] || continue
  [[ "${revision}" == "${successful_revision}" ]] && continue
  "${gcloud_bin}" run revisions delete "${revision}" \
    --project "${project_id}" \
    --region "${region}" \
    --quiet
  deleted_count=$((deleted_count + 1))
done < <(
  jq -r '
    sort_by(.metadata.creationTimestamp)
    | reverse
    | .[].metadata.name
  ' <<<"${revisions_json}"
)

printf 'Retained successful Cloud Run revision %s for %s.\n' \
  "${successful_revision}" \
  "${service}"
printf 'Deleted %s older Cloud Run revision(s).\n' "${deleted_count}"
