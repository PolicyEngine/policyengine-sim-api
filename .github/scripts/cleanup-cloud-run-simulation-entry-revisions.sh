#!/usr/bin/env bash

# Retain the three most recent entrypoint revisions from completed deployments.
# The fixed pe-retained-* tags carry successful deployment history across runs.
# Existing candidate tags are preserved for retained revisions so workflow
# outputs remain usable. All tags are removed from older revisions before those
# revisions are deleted.

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_ENTRYPOINT_GCP_PROJECT_ID:?SIMULATION_ENTRYPOINT_GCP_PROJECT_ID is required}"
region="${SIMULATION_ENTRYPOINT_GCP_REGION:-us-central1}"
service="${SIMULATION_ENTRYPOINT_SERVICE:?SIMULATION_ENTRYPOINT_SERVICE is required}"
successful_revision="${SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION:?SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION is required}"
previous_successful_revision="${SIMULATION_ENTRYPOINT_PREVIOUS_SUCCESSFUL_REVISION:-}"
retain_count="${SIMULATION_ENTRYPOINT_REVISIONS_TO_RETAIN:-3}"

fail() {
  printf 'Cloud Run revision cleanup error: %s\n' "$1" >&2
  exit 1
}

[[ "${retain_count}" =~ ^[1-9][0-9]*$ ]] \
  || fail "SIMULATION_ENTRYPOINT_REVISIONS_TO_RETAIN must be a positive integer"
[[ "${successful_revision}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || fail "SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION is invalid"
if [[ -n "${previous_successful_revision}" ]]; then
  [[ "${previous_successful_revision}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || fail "SIMULATION_ENTRYPOINT_PREVIOUS_SUCCESSFUL_REVISION is invalid"
fi

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

ready_revisions=()
while IFS= read -r revision; do
  [[ -n "${revision}" ]] && ready_revisions+=("${revision}")
done < <(
  jq -r '
    map(
      select(
        any(
          .status.conditions[]?;
          .type == "Ready" and .status == "True"
        )
      )
    )
    | sort_by(.metadata.creationTimestamp)
    | reverse
    | .[].metadata.name
  ' <<<"${revisions_json}"
)

is_ready_revision() {
  local candidate="$1"
  local revision
  for revision in ${ready_revisions[@]+"${ready_revisions[@]}"}; do
    if [[ "${revision}" == "${candidate}" ]]; then
      return 0
    fi
  done
  return 1
}

is_retained_revision() {
  local candidate="$1"
  local revision
  for revision in ${retained_revisions[@]+"${retained_revisions[@]}"}; do
    if [[ "${revision}" == "${candidate}" ]]; then
      return 0
    fi
  done
  return 1
}

retained_revisions=()
append_retained_revision() {
  local candidate="$1"
  local required="${2:-false}"

  [[ -n "${candidate}" ]] || return 0
  if ! is_ready_revision "${candidate}"; then
    if [[ "${required}" == "true" ]]; then
      fail "${candidate} is not a ready revision of ${service}"
    fi
    return 0
  fi
  if is_retained_revision "${candidate}"; then
    return 0
  fi
  if ((${#retained_revisions[@]} >= retain_count)); then
    if [[ "${required}" == "true" ]]; then
      fail "more than ${retain_count} ready revisions require retention"
    fi
    return 0
  fi
  retained_revisions+=("${candidate}")
}

# The workflow invokes this script only after the candidate passes every
# configured deployment check, so this is the newest successful revision.
append_retained_revision "${successful_revision}" true

# Never remove a revision that currently receives service traffic. This also
# protects the stable revision in staging, where candidates are verified but
# are not promoted automatically.
while IFS= read -r revision; do
  append_retained_revision "${revision}" true
done < <(
  jq -r '
    .status.traffic[]?
    | select((.percent // 0) > 0)
    | .revisionName
    | select(type == "string" and length > 0)
  ' <<<"${service_json}"
)

# Production supplies the revision that had all service traffic immediately
# before promotion. Keeping it explicitly preserves the first rollback option
# while retention tags are bootstrapped.
if [[ -n "${previous_successful_revision}" ]]; then
  append_retained_revision "${previous_successful_revision}" true
fi

# Reuse the successful history written by previous cleanup runs before falling
# back to creation order for the initial rollout of this script.
retention_index=1
while ((retention_index <= retain_count)); do
  retained_revision="$(
    jq -r --arg tag "pe-retained-${retention_index}" '
      first(
        .status.traffic[]?
        | select(.tag == $tag)
        | .revisionName
      ) // empty
    ' <<<"${service_json}"
  )"
  append_retained_revision "${retained_revision}"
  retention_index=$((retention_index + 1))
done

for revision in ${ready_revisions[@]+"${ready_revisions[@]}"}; do
  append_retained_revision "${revision}"
done

((${#retained_revisions[@]} > 0)) \
  || fail "no ready revisions were found for ${service}"

# Preserve the deployment-specific tags of retained revisions. They back the
# candidate URLs emitted by the workflow and remain useful during rollback.
tag_assignments=()
while IFS=$'\t' read -r tag revision; do
  [[ -n "${tag}" && -n "${revision}" ]] || continue
  [[ "${tag}" == pe-retained-* ]] && continue
  if is_retained_revision "${revision}"; then
    tag_assignments+=("${tag}=${revision}")
  fi
done < <(
  jq -r '
    .status.traffic[]?
    | select(
        (.tag | type) == "string"
        and (.revisionName | type) == "string"
      )
    | [.tag, .revisionName]
    | @tsv
  ' <<<"${service_json}"
)

retention_index=1
for revision in ${retained_revisions[@]+"${retained_revisions[@]}"}; do
  tag_assignments+=("pe-retained-${retention_index}=${revision}")
  retention_index=$((retention_index + 1))
done

tags_csv="$(IFS=,; printf '%s' "${tag_assignments[*]}")"
"${gcloud_bin}" run services update-traffic "${service}" \
  --project "${project_id}" \
  --region "${region}" \
  --set-tags "${tags_csv}" \
  --quiet

deleted_count=0
while IFS= read -r revision; do
  [[ -n "${revision}" ]] || continue
  if is_retained_revision "${revision}"; then
    continue
  fi
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

printf 'Retained %s ready Cloud Run revision(s) for %s: %s\n' \
  "${#retained_revisions[@]}" \
  "${service}" \
  "$(IFS=,; printf '%s' "${retained_revisions[*]}")"
printf 'Deleted %s older Cloud Run revision(s).\n' "${deleted_count}"
