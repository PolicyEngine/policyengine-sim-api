#!/usr/bin/env bash

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_ENTRYPOINT_GCP_PROJECT_ID:?SIMULATION_ENTRYPOINT_GCP_PROJECT_ID is required}"
region="${SIMULATION_ENTRYPOINT_GCP_REGION:-us-central1}"
production_domain="${SIMULATION_ENTRYPOINT_PRODUCTION_DOMAIN:-simulation.api.policyengine.org}"
staging_domain="${SIMULATION_ENTRYPOINT_STAGING_DOMAIN:-staging.simulation.api.policyengine.org}"
dry_run="${SIMULATION_ENTRYPOINT_DOMAINS_DRY_RUN:-0}"

run() {
  if [ "${dry_run}" = "1" ]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

exists() {
  if [ "${dry_run}" = "1" ]; then
    return 1
  fi
  "$@" >/dev/null 2>&1
}

ensure_mapping() {
  local service="${1:?service is required}"
  local domain="${2:?domain is required}"

  if ! exists "${gcloud_bin}" beta run domain-mappings describe \
    --project "${project_id}" \
    --region "${region}" \
    --domain "${domain}"; then
    run "${gcloud_bin}" beta run domain-mappings create \
      --project "${project_id}" \
      --region "${region}" \
      --service "${service}" \
      --domain "${domain}"
  fi
}

ensure_mapping policyengine-simulation-entrypoint-staging "${staging_domain}"
ensure_mapping policyengine-simulation-entrypoint "${production_domain}"

printf '\nInspect the mappings for the DNS records that must be published:\n'
printf '  %s\n' "${staging_domain}" "${production_domain}"
