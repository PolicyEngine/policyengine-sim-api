#!/usr/bin/env bash
# Synchronize Stage 12 GCP resources into the Modal environment.

set -euo pipefail
set +x

modal_environment="${1:?Modal environment is required}"
: "${STAGE12_GCP_PROJECT_ID:?STAGE12_GCP_PROJECT_ID is required}"
: "${STAGE12_GCP_CREDENTIALS_SECRET_NAME:?STAGE12_GCP_CREDENTIALS_SECRET_NAME is required}"
: "${STAGE12_ARTIFACT_BUCKET:?STAGE12_ARTIFACT_BUCKET is required}"
: "${STAGE12_PERSISTENCE_API_URL:?STAGE12_PERSISTENCE_API_URL is required}"
: "${MODAL_TOKEN_ID:?MODAL_TOKEN_ID is required}"
: "${MODAL_TOKEN_SECRET:?MODAL_TOKEN_SECRET is required}"

if [[ ! "${modal_environment}" =~ ^(staging|main)$ ]]; then
  echo "Modal environment must be staging or main" >&2
  exit 1
fi

credentials_file="$(mktemp)"
runtime_modal_file="$(mktemp)"
credentials_modal_file="$(mktemp)"
trap 'rm -f "${credentials_file}" "${runtime_modal_file}" "${credentials_modal_file}"' EXIT

gcloud secrets versions access latest \
  --secret "${STAGE12_GCP_CREDENTIALS_SECRET_NAME}" \
  --project "${STAGE12_GCP_PROJECT_ID}" >"${credentials_file}"

jq -n \
  --arg artifact_bucket "${STAGE12_ARTIFACT_BUCKET}" \
  --arg persistence_api_url "${STAGE12_PERSISTENCE_API_URL}" \
  '{STAGE12_ARTIFACT_BUCKET: $artifact_bucket, STAGE12_PERSISTENCE_API_URL: $persistence_api_url}' \
  >"${runtime_modal_file}"
jq -n \
  --rawfile credentials "${credentials_file}" \
  '{GOOGLE_APPLICATION_CREDENTIALS_JSON: $credentials}' \
  >"${credentials_modal_file}"

uv run modal secret create stage12-evaluation-runtime \
  --from-json "${runtime_modal_file}" \
  --env "${modal_environment}" \
  --force
uv run modal secret create stage12-evaluation-gcp-credentials \
  --from-json "${credentials_modal_file}" \
  --env "${modal_environment}" \
  --force

echo "Stage 12 Modal secrets synchronized in ${modal_environment}."
