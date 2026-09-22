#!/usr/bin/env bash
# Validate pre-provisioned Stage 12 resources with bounded disposable canaries.

set -euo pipefail
set +x

: "${STAGE12_ENVIRONMENT:?STAGE12_ENVIRONMENT is required}"
: "${STAGE12_GCP_PROJECT_ID:?STAGE12_GCP_PROJECT_ID is required}"
: "${STAGE12_ARTIFACT_BUCKET:?STAGE12_ARTIFACT_BUCKET is required}"
: "${STAGE12_ENTRYPOINT_SERVICE_ACCOUNT:?STAGE12_ENTRYPOINT_SERVICE_ACCOUNT is required}"
: "${STAGE12_DEPLOY_SERVICE_ACCOUNT:?STAGE12_DEPLOY_SERVICE_ACCOUNT is required}"
: "${STAGE12_MODAL_SERVICE_ACCOUNT:?STAGE12_MODAL_SERVICE_ACCOUNT is required}"
: "${STAGE12_MODAL_TOKEN_ID_SECRET_NAME:?STAGE12_MODAL_TOKEN_ID_SECRET_NAME is required}"
: "${STAGE12_MODAL_TOKEN_SECRET_SECRET_NAME:?STAGE12_MODAL_TOKEN_SECRET_SECRET_NAME is required}"
: "${STAGE12_DATABASE_URL_SECRET_NAME:?STAGE12_DATABASE_URL_SECRET_NAME is required}"
: "${STAGE12_GCP_CREDENTIALS_SECRET_NAME:?STAGE12_GCP_CREDENTIALS_SECRET_NAME is required}"

if [[ ! "${STAGE12_ENVIRONMENT}" =~ ^(staging|production)$ ]]; then
  echo "STAGE12_ENVIRONMENT must be staging or production" >&2
  exit 1
fi
if [[ ! "${STAGE12_GCP_PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,61}[a-z0-9]$ ]]; then
  echo "STAGE12_GCP_PROJECT_ID is not a valid explicit project name" >&2
  exit 1
fi
if [[ ! "${STAGE12_ARTIFACT_BUCKET}" =~ ^[a-z0-9][a-z0-9._-]{2,61}[a-z0-9]$ ]] ||
  [[ ! "${STAGE12_ARTIFACT_BUCKET}" =~ ${STAGE12_ENVIRONMENT} ]]; then
  echo "STAGE12_ARTIFACT_BUCKET must be valid and identify the environment" >&2
  exit 1
fi
for account in \
  "${STAGE12_ENTRYPOINT_SERVICE_ACCOUNT}" \
  "${STAGE12_DEPLOY_SERVICE_ACCOUNT}" \
  "${STAGE12_MODAL_SERVICE_ACCOUNT}"; do
  if [[ ! "${account}" =~ ^[a-z0-9-]+@${STAGE12_GCP_PROJECT_ID}\.iam\.gserviceaccount\.com$ ]]; then
    echo "Invalid or cross-project service account: ${account}" >&2
    exit 1
  fi
done
for secret_name in \
  "${STAGE12_MODAL_TOKEN_ID_SECRET_NAME}" \
  "${STAGE12_MODAL_TOKEN_SECRET_SECRET_NAME}" \
  "${STAGE12_DATABASE_URL_SECRET_NAME}" \
  "${STAGE12_GCP_CREDENTIALS_SECRET_NAME}"; do
  if [[ ! "${secret_name}" =~ ^[a-zA-Z0-9_-]{1,255}$ ]]; then
    echo "Invalid Stage 12 Secret Manager secret name" >&2
    exit 1
  fi
  enabled_version="$(
    gcloud secrets versions list "${secret_name}" \
      --project "${STAGE12_GCP_PROJECT_ID}" \
      --filter 'state=ENABLED' \
      --format 'value(name)' \
      --limit 1
  )"
  if [[ -z "${enabled_version}" ]]; then
    echo "Stage 12 secret has no enabled version: ${secret_name}" >&2
    exit 1
  fi
done

database_url_file="$(mktemp)"
credentials_file="$(mktemp)"
canary_source_file="$(mktemp)"
canary_download_file="$(mktemp)"
runtime_gcloud_config="$(mktemp -d)"
canary_object=""
runtime_gcloud() {
  # The GitHub auth action exports credentials for the deployment identity.
  # Remove them only here so the activated Modal worker account controls this check.
  env \
    -u CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u GOOGLE_GHA_CREDS_PATH \
    CLOUDSDK_CONFIG="${runtime_gcloud_config}" \
    gcloud "$@"
}
cleanup() {
  if [[ -n "${canary_object}" ]]; then
    runtime_gcloud --account="${STAGE12_MODAL_SERVICE_ACCOUNT}" storage rm \
      "${canary_object}" --quiet >/dev/null 2>&1 || true
  fi
  rm -f \
    "${database_url_file}" \
    "${credentials_file}" \
    "${canary_source_file}" \
    "${canary_download_file}"
  rm -rf "${runtime_gcloud_config}"
}
trap cleanup EXIT
gcloud secrets versions access latest \
  --secret "${STAGE12_DATABASE_URL_SECRET_NAME}" \
  --project "${STAGE12_GCP_PROJECT_ID}" >"${database_url_file}"
gcloud secrets versions access latest \
  --secret "${STAGE12_GCP_CREDENTIALS_SECRET_NAME}" \
  --project "${STAGE12_GCP_PROJECT_ID}" >"${credentials_file}"

expected_role="policyengine_v2_runtime"
database_url="$(<"${database_url_file}")"
# Supabase's session pooler appends the 20-character project reference to the
# PostgreSQL role in the URL. PostgreSQL still authenticates the connection as
# the exact role above; the live current_user check below verifies that fact.
if [[ ! "${database_url}" =~ ^postgresql://${expected_role}\.[a-z0-9]{20}:[^@[:space:]]+@[^/:[:space:]]+\.pooler\.supabase\.com:5432/postgres\?sslmode=require$ ]]; then
  echo "Shared API v2 runtime database URL identifies an unexpected target" >&2
  exit 1
fi
jq -e --arg expected "${STAGE12_MODAL_SERVICE_ACCOUNT}" \
  '.type == "service_account" and .client_email == $expected' \
  "${credentials_file}" >/dev/null

require_secret_accessor() {
  local secret_name="$1"
  local service_account="$2"
  gcloud secrets get-iam-policy "${secret_name}" \
    --project "${STAGE12_GCP_PROJECT_ID}" \
    --format json |
    jq -e --arg member "serviceAccount:${service_account}" '
      any(
        .bindings[]?;
        .role == "roles/secretmanager.secretAccessor"
        and any(.members[]?; . == $member)
      )
    ' >/dev/null
}

for entrypoint_secret in \
  "${STAGE12_MODAL_TOKEN_ID_SECRET_NAME}" \
  "${STAGE12_MODAL_TOKEN_SECRET_SECRET_NAME}" \
  "${STAGE12_DATABASE_URL_SECRET_NAME}"; do
  require_secret_accessor \
    "${entrypoint_secret}" \
    "${STAGE12_ENTRYPOINT_SERVICE_ACCOUNT}"
done
require_secret_accessor \
  "${STAGE12_DATABASE_URL_SECRET_NAME}" \
  "${STAGE12_DEPLOY_SERVICE_ACCOUNT}"
require_secret_accessor \
  "${STAGE12_GCP_CREDENTIALS_SECRET_NAME}" \
  "${STAGE12_DEPLOY_SERVICE_ACCOUNT}"

uv run --project projects/policyengine-simulation-entry \
  python -m policyengine_simulation_entry.stage12_infrastructure \
  --database-url-file "${database_url_file}" \
  --expected-role "${expected_role}" \
  --environment "${STAGE12_ENVIRONMENT}"

runtime_gcloud auth activate-service-account \
  "${STAGE12_MODAL_SERVICE_ACCOUNT}" \
  --key-file "${credentials_file}" \
  --project "${STAGE12_GCP_PROJECT_ID}" \
  --quiet >/dev/null
canary_id="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-${RANDOM}"
canary_object="gs://${STAGE12_ARTIFACT_BUCKET}/stage-12-runs/_deployment-validation/${STAGE12_ENVIRONMENT}/${canary_id}.txt"
printf 'stage12-storage-validation:%s\n' "${canary_id}" >"${canary_source_file}"
runtime_gcloud --account="${STAGE12_MODAL_SERVICE_ACCOUNT}" storage cp \
  "${canary_source_file}" "${canary_object}" --quiet
runtime_gcloud --account="${STAGE12_MODAL_SERVICE_ACCOUNT}" storage cp \
  "${canary_object}" "${canary_download_file}" --quiet
cmp "${canary_source_file}" "${canary_download_file}"
runtime_gcloud --account="${STAGE12_MODAL_SERVICE_ACCOUNT}" storage rm \
  "${canary_object}" --quiet
canary_object=""

echo "Pre-provisioned Stage 12 database, secret, and storage access is verified."
