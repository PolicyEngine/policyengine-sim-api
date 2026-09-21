#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'Cloud Run deployment configuration error: %s\n' "$1" >&2
  exit 1
}

require_environment_variable() {
  local variable_name="$1"
  [[ -n "${!variable_name:-}" ]] \
    || fail "${variable_name} must be set"
}

validate_secret_reference() {
  local reference="$1"
  [[ "${reference}" =~ ^[A-Za-z0-9_-]{1,255}$ ]] \
    || [[ "${reference}" =~ ^projects/[A-Za-z0-9-]+/secrets/[A-Za-z0-9_-]{1,255}$ ]] \
    || fail "invalid Secret Manager reference: ${reference}"
}

resolve_secret_reference() {
  local reference="$1"
  local secret_project="${PROJECT_ID}"
  local secret_name="${reference}"
  local highest_enabled_version=0
  local listed_version
  local version
  local versions_output

  validate_secret_reference "${reference}"
  if [[ "${reference}" =~ ^projects/([A-Za-z0-9-]+)/secrets/([A-Za-z0-9_-]{1,255})$ ]]; then
    secret_project="${BASH_REMATCH[1]}"
    secret_name="${BASH_REMATCH[2]}"
  fi

  if ! versions_output="$(
    gcloud secrets versions list "${secret_name}" \
      --project "${secret_project}" \
      --filter="state=ENABLED" \
      --format="value(name)"
  )"; then
    fail "could not list enabled Secret Manager versions for ${reference}"
  fi

  while IFS= read -r listed_version; do
    [[ -n "${listed_version}" ]] || continue
    version="${listed_version##*/}"
    [[ "${version}" =~ ^[1-9][0-9]*$ ]] \
      || fail "Secret Manager returned an invalid version for ${reference}"
    if ((version > highest_enabled_version)); then
      highest_enabled_version="${version}"
    fi
  done <<< "${versions_output}"

  ((highest_enabled_version > 0)) \
    || fail "${reference} has no enabled Secret Manager version"
  printf '%s:%s' "${reference}" "${highest_enabled_version}"
}

required_variables=(
  PROJECT_ID
  REGION
  IMAGE
  TAG
  DEPLOY_STAGE12_V2
  APP_ENVIRONMENT
  MODAL_ENVIRONMENT
  ENTRYPOINT_SERVICE
  ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT
  ENTRYPOINT_MIN_INSTANCES
  ENTRYPOINT_MAX_INSTANCES
  SIMULATION_ENTRYPOINT_AUTH_ISSUER_VALUE
  SIMULATION_ENTRYPOINT_AUTH_AUDIENCE_VALUE
  OLD_GATEWAY_URL_VALUE
  OLD_GATEWAY_AUTH_ISSUER_VALUE
  OLD_GATEWAY_AUTH_AUDIENCE_VALUE
  OLD_GATEWAY_AUTH_CLIENT_ID_VALUE
  OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME
  RUNNER_TEMP
)
for variable_name in "${required_variables[@]}"; do
  require_environment_variable "${variable_name}"
done

[[ "${DEPLOY_STAGE12_V2}" == "true" || "${DEPLOY_STAGE12_V2}" == "false" ]] \
  || fail "DEPLOY_STAGE12_V2 must be true or false"
case "${APP_ENVIRONMENT}:${MODAL_ENVIRONMENT}" in
  staging:staging | production:main) ;;
  *) fail "APP_ENVIRONMENT and MODAL_ENVIRONMENT do not describe a supported deployment" ;;
esac
[[ "${PROJECT_ID}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] \
  || fail "PROJECT_ID is invalid"
[[ "${REGION}" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] \
  || fail "REGION is invalid"
[[ "${ENTRYPOINT_SERVICE}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || fail "ENTRYPOINT_SERVICE is invalid"
[[ "${TAG}" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
  || fail "TAG is invalid"
[[ "${ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.gserviceaccount\.com$ ]] \
  || fail "ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT is invalid"
[[ "${ENTRYPOINT_MIN_INSTANCES}" =~ ^[0-9]+$ ]] \
  || fail "ENTRYPOINT_MIN_INSTANCES must be a non-negative integer"
[[ "${ENTRYPOINT_MAX_INSTANCES}" =~ ^[0-9]+$ ]] \
  || fail "ENTRYPOINT_MAX_INSTANCES must be a non-negative integer"
((10#${ENTRYPOINT_MAX_INSTANCES} >= 10#${ENTRYPOINT_MIN_INSTANCES})) \
  || fail "ENTRYPOINT_MAX_INSTANCES must be at least ENTRYPOINT_MIN_INSTANCES"

runtime_secrets=(
  "OLD_GATEWAY_AUTH_CLIENT_SECRET=$(
    resolve_secret_reference "${OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME}"
  )"
)

if [[ "${DEPLOY_STAGE12_V2}" == "true" ]]; then
  stage12_variables=(
    STAGE12_ENABLED_VALUE
    STAGE12_ARTIFACT_BUCKET_VALUE
    MODAL_TOKEN_ID_SECRET_NAME
    MODAL_TOKEN_SECRET_SECRET_NAME
    STAGE12_DATABASE_URL_SECRET_NAME
  )
  for variable_name in "${stage12_variables[@]}"; do
    require_environment_variable "${variable_name}"
  done
  [[ "${STAGE12_ENABLED_VALUE}" == "0" || "${STAGE12_ENABLED_VALUE}" == "1" ]] \
    || fail "STAGE12_ENABLED_VALUE must be 0 or 1"
  [[ "${STAGE12_ARTIFACT_BUCKET_VALUE}" =~ ^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$ ]] \
    || fail "STAGE12_ARTIFACT_BUCKET_VALUE is invalid"

  runtime_secrets+=(
    "MODAL_TOKEN_ID=$(resolve_secret_reference "${MODAL_TOKEN_ID_SECRET_NAME}")"
    "MODAL_TOKEN_SECRET=$(resolve_secret_reference "${MODAL_TOKEN_SECRET_SECRET_NAME}")"
    "STAGE12_DATABASE_URL=$(resolve_secret_reference "${STAGE12_DATABASE_URL_SECRET_NAME}")"
  )
fi

runtime_environment_file="${RUNNER_TEMP}/simulation-entry-runtime-environment.yaml"
trap 'rm -f "${runtime_environment_file}"' EXIT
jq -n '
  {
    APP_ENVIRONMENT: env.APP_ENVIRONMENT,
    SIMULATION_ENTRYPOINT_AUTH_REQUIRED: "1",
    SIMULATION_ENTRYPOINT_AUTH_ISSUER: env.SIMULATION_ENTRYPOINT_AUTH_ISSUER_VALUE,
    SIMULATION_ENTRYPOINT_AUTH_AUDIENCE: env.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE_VALUE,
    OLD_GATEWAY_URL: env.OLD_GATEWAY_URL_VALUE,
    OLD_GATEWAY_AUTH_ISSUER: env.OLD_GATEWAY_AUTH_ISSUER_VALUE,
    OLD_GATEWAY_AUTH_AUDIENCE: env.OLD_GATEWAY_AUTH_AUDIENCE_VALUE,
    OLD_GATEWAY_AUTH_CLIENT_ID: env.OLD_GATEWAY_AUTH_CLIENT_ID_VALUE,
    STAGE12_ENABLED: (
      if env.DEPLOY_STAGE12_V2 == "true" then env.STAGE12_ENABLED_VALUE else "0" end
    )
  }
  + if env.DEPLOY_STAGE12_V2 == "true" then {
      STAGE12_V2_MANIFEST_NAME: "simulation-api-v2-version-manifest",
      STAGE12_V2_MANIFEST_ENVIRONMENT: env.MODAL_ENVIRONMENT,
      STAGE12_ARTIFACT_BUCKET: env.STAGE12_ARTIFACT_BUCKET_VALUE
    } else {} end
' > "${runtime_environment_file}"

runtime_secrets_csv="$(IFS=,; printf '%s' "${runtime_secrets[*]}")"
gcloud run deploy "${ENTRYPOINT_SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE}" \
  --tag "${TAG}" \
  --no-traffic \
  --allow-unauthenticated \
  --service-account "${ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT}" \
  --env-vars-file "${runtime_environment_file}" \
  --set-secrets "${runtime_secrets_csv}" \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 80 \
  --timeout 60 \
  --min-instances "${ENTRYPOINT_MIN_INSTANCES}" \
  --max-instances "${ENTRYPOINT_MAX_INSTANCES}"
