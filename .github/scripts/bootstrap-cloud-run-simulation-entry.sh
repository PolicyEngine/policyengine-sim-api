#!/usr/bin/env bash

set -euo pipefail

gcloud_bin="${GCLOUD_BIN:-gcloud}"
project_id="${SIMULATION_ENTRYPOINT_GCP_PROJECT_ID:?SIMULATION_ENTRYPOINT_GCP_PROJECT_ID is required}"
billing_account="${SIMULATION_ENTRYPOINT_GCP_BILLING_ACCOUNT:-}"
region="${SIMULATION_ENTRYPOINT_GCP_REGION:-us-central1}"
repository="${SIMULATION_ENTRYPOINT_ARTIFACT_REPOSITORY:-policyengine-simulation-entry}"
github_repository="${SIMULATION_ENTRYPOINT_GITHUB_REPOSITORY:-PolicyEngine/policyengine-sim-api}"
pool_id="${SIMULATION_ENTRYPOINT_WIF_POOL_ID:-simulation-entry-github}"
provider_id="${SIMULATION_ENTRYPOINT_WIF_PROVIDER_ID:-github}"
dry_run="${SIMULATION_ENTRYPOINT_BOOTSTRAP_DRY_RUN:-0}"
workflow_ref="${github_repository}/.github/workflows/cloud-run-simulation-entry.yml@refs/heads/main"

deployer_account_id="sim-entry-gh-deployer"
staging_runtime_account_id="sim-entry-stg-runtime"
production_runtime_account_id="sim-entry-prod-runtime"
staging_secret="simulation-entry-old-gateway-client-secret-staging"
production_secret="simulation-entry-old-gateway-client-secret-production"

if [ "${#project_id}" -gt 30 ] ||
  ! [[ "${project_id}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  printf 'SIMULATION_ENTRYPOINT_GCP_PROJECT_ID must be a valid 6-30 character GCP project ID\n' >&2
  exit 2
fi

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

if ! exists "${gcloud_bin}" projects describe "${project_id}"; then
  project_args=(
    projects create "${project_id}"
    --name "PE Simulation Entrypoint"
  )
  if [ -n "${SIMULATION_ENTRYPOINT_GCP_FOLDER_ID:-}" ]; then
    project_args+=(--folder "${SIMULATION_ENTRYPOINT_GCP_FOLDER_ID}")
  elif [ -n "${SIMULATION_ENTRYPOINT_GCP_ORGANIZATION_ID:-}" ]; then
    project_args+=(--organization "${SIMULATION_ENTRYPOINT_GCP_ORGANIZATION_ID}")
  fi
  run "${gcloud_bin}" "${project_args[@]}"
fi

if [ -n "${billing_account}" ]; then
  run "${gcloud_bin}" billing projects link "${project_id}" \
    --billing-account "${billing_account}"
fi

run "${gcloud_bin}" services enable \
  artifactregistry.googleapis.com \
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  sts.googleapis.com \
  --project "${project_id}"

if ! exists "${gcloud_bin}" artifacts repositories describe "${repository}" \
  --project "${project_id}" \
  --location "${region}"; then
  run "${gcloud_bin}" artifacts repositories create "${repository}" \
    --project "${project_id}" \
    --location "${region}" \
    --repository-format docker \
    --description "Cloud Run images for the PolicyEngine Simulation Entrypoint"
fi

ensure_service_account() {
  local account_id="${1:?service account ID is required}"
  local display_name="${2:?display name is required}"
  local email="${account_id}@${project_id}.iam.gserviceaccount.com"
  if ! exists "${gcloud_bin}" iam service-accounts describe "${email}" \
    --project "${project_id}"; then
    run "${gcloud_bin}" iam service-accounts create "${account_id}" \
      --project "${project_id}" \
      --display-name "${display_name}"
  fi
}

ensure_service_account "${deployer_account_id}" \
  "Simulation Entrypoint GitHub deployer"
ensure_service_account "${staging_runtime_account_id}" \
  "Simulation Entrypoint staging runtime"
ensure_service_account "${production_runtime_account_id}" \
  "Simulation Entrypoint production runtime"

deployer_email="${deployer_account_id}@${project_id}.iam.gserviceaccount.com"
staging_runtime_email="${staging_runtime_account_id}@${project_id}.iam.gserviceaccount.com"
production_runtime_email="${production_runtime_account_id}@${project_id}.iam.gserviceaccount.com"

for role in \
  roles/artifactregistry.writer \
  roles/run.admin \
  roles/secretmanager.viewer \
  roles/serviceusage.serviceUsageConsumer; do
  run "${gcloud_bin}" projects add-iam-policy-binding "${project_id}" \
    --member "serviceAccount:${deployer_email}" \
    --role "${role}" \
    --condition=None \
    --quiet
done

for runtime_email in "${staging_runtime_email}" "${production_runtime_email}"; do
  run "${gcloud_bin}" iam service-accounts add-iam-policy-binding \
    "${runtime_email}" \
    --project "${project_id}" \
    --member "serviceAccount:${deployer_email}" \
    --role roles/iam.serviceAccountUser \
    --quiet
done

ensure_secret() {
  local secret="${1:?secret name is required}"
  if ! exists "${gcloud_bin}" secrets describe "${secret}" \
    --project "${project_id}"; then
    run "${gcloud_bin}" secrets create "${secret}" \
      --project "${project_id}" \
      --replication-policy automatic
  fi
}

ensure_secret "${staging_secret}"
ensure_secret "${production_secret}"

run "${gcloud_bin}" secrets add-iam-policy-binding "${staging_secret}" \
  --project "${project_id}" \
  --member "serviceAccount:${staging_runtime_email}" \
  --role roles/secretmanager.secretAccessor \
  --quiet
run "${gcloud_bin}" secrets add-iam-policy-binding "${production_secret}" \
  --project "${project_id}" \
  --member "serviceAccount:${production_runtime_email}" \
  --role roles/secretmanager.secretAccessor \
  --quiet

if ! exists "${gcloud_bin}" iam workload-identity-pools describe "${pool_id}" \
  --project "${project_id}" \
  --location global; then
  run "${gcloud_bin}" iam workload-identity-pools create "${pool_id}" \
    --project "${project_id}" \
    --location global \
    --display-name "Simulation Entrypoint GitHub Actions"
fi

provider_args=(
  "${provider_id}"
  --project "${project_id}"
  --location global
  --workload-identity-pool "${pool_id}"
  --issuer-uri "https://token.actions.githubusercontent.com"
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref,attribute.workflow_ref=assertion.workflow_ref"
  --attribute-condition "assertion.repository == '${github_repository}' && assertion.ref == 'refs/heads/main' && assertion.workflow_ref == '${workflow_ref}'"
)

if exists "${gcloud_bin}" iam workload-identity-pools providers describe \
  "${provider_id}" \
  --project "${project_id}" \
  --location global \
  --workload-identity-pool "${pool_id}"; then
  run "${gcloud_bin}" iam workload-identity-pools providers update-oidc \
    "${provider_args[@]}" \
    --display-name "PolicyEngine simulation repository" \
    --quiet
else
  run "${gcloud_bin}" iam workload-identity-pools providers create-oidc \
    "${provider_args[@]}" \
    --display-name "PolicyEngine simulation repository"
fi

if [ "${dry_run}" = "1" ]; then
  project_number="000000000000"
else
  project_number="$("${gcloud_bin}" projects describe "${project_id}" \
    --format 'value(projectNumber)')"
fi
run "${gcloud_bin}" iam service-accounts add-iam-policy-binding \
  "${deployer_email}" \
  --project "${project_id}" \
  --member "principalSet://iam.googleapis.com/projects/${project_number}/locations/global/workloadIdentityPools/${pool_id}/attribute.repository/${github_repository}" \
  --role roles/iam.workloadIdentityUser \
  --quiet

provider_name="projects/${project_number}/locations/global/workloadIdentityPools/${pool_id}/providers/${provider_id}"

printf '\nBootstrap complete. Configure these GitHub values:\n'
printf '  SIMULATION_ENTRYPOINT_GCP_PROJECT_ID=%s\n' "${project_id}"
printf '  SIMULATION_ENTRYPOINT_GCP_REGION=%s\n' "${region}"
printf '  SIMULATION_ENTRYPOINT_ARTIFACT_REPOSITORY=%s\n' "${repository}"
printf '  SIMULATION_ENTRYPOINT_GCP_WIF_PROVIDER=%s\n' "${provider_name}"
printf '  SIMULATION_ENTRYPOINT_GCP_DEPLOY_SERVICE_ACCOUNT=%s\n' "${deployer_email}"
printf '  staging SIMULATION_ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT=%s\n' "${staging_runtime_email}"
printf '  production SIMULATION_ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT=%s\n' "${production_runtime_email}"
printf '\nAdd one secret version to each empty Secret Manager secret before deploying.\n'
