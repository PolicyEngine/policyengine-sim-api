#!/usr/bin/env bash

# Operator-run production/staging command. GitHub Actions intentionally never
# invokes this script or changes Cloud Run traffic.

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_GCP_PROJECT_ID:?SIMULATION_GCP_PROJECT_ID is required}"
region="${SIMULATION_GCP_REGION:-us-central1}"
environment="${SIMULATION_DEPLOYMENT_ENVIRONMENT:?SIMULATION_DEPLOYMENT_ENVIRONMENT is required}"
revision="${SIMULATION_TARGET_REVISION:?SIMULATION_TARGET_REVISION is required}"
dry_run="${SIMULATION_TRAFFIC_DRY_RUN:-0}"

case "${environment}" in
  staging)
    service="policyengine-simulation-api-staging"
    ;;
  production)
    service="policyengine-simulation-api"
    ;;
  *)
    printf 'SIMULATION_DEPLOYMENT_ENVIRONMENT must be staging or production\n' >&2
    exit 2
    ;;
esac

if [ "${dry_run}" = "1" ]; then
  printf '+ %q' "${gcloud_bin}"
  printf ' %q' run services update-traffic "${service}" \
    --project "${project_id}" \
    --region "${region}" \
    --to-revisions "${revision}=100"
  printf '\n'
  exit 0
fi

revision_json="$("${gcloud_bin}" run revisions describe "${revision}" \
  --project "${project_id}" \
  --region "${region}" \
  --format=json)"
revision_service="$(jq -r \
  '.metadata.labels["serving.knative.dev/service"] // empty' \
  <<<"${revision_json}")"

if [ "${revision_service}" != "${service}" ]; then
  printf 'Revision %s belongs to %s, not %s\n' \
    "${revision}" "${revision_service:-an unknown service}" "${service}" >&2
  exit 2
fi

if ! jq -e \
  '.status.conditions[]? | select(.type == "Ready" and .status == "True")' \
  >/dev/null <<<"${revision_json}"; then
  printf 'Revision %s is not Ready\n' "${revision}" >&2
  exit 2
fi

"${gcloud_bin}" run services update-traffic "${service}" \
  --project "${project_id}" \
  --region "${region}" \
  --to-revisions "${revision}=100"
