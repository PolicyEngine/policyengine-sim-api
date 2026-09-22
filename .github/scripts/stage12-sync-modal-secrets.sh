#!/usr/bin/env bash
# Synchronize Stage 12 GCP resources into the Modal environment.

set -euo pipefail
set +x

modal_environment="${1:?Modal environment is required}"
: "${STAGE12_GCP_PROJECT_ID:?STAGE12_GCP_PROJECT_ID is required}"
: "${STAGE12_DATABASE_URL_SECRET_NAME:?STAGE12_DATABASE_URL_SECRET_NAME is required}"
: "${STAGE12_GCP_CREDENTIALS_SECRET_NAME:?STAGE12_GCP_CREDENTIALS_SECRET_NAME is required}"
: "${STAGE12_ARTIFACT_BUCKET:?STAGE12_ARTIFACT_BUCKET is required}"
: "${MODAL_TOKEN_ID:?MODAL_TOKEN_ID is required}"
: "${MODAL_TOKEN_SECRET:?MODAL_TOKEN_SECRET is required}"

if [[ ! "${modal_environment}" =~ ^(staging|main)$ ]]; then
  echo "Modal environment must be staging or main" >&2
  exit 1
fi

database_url_file="$(mktemp)"
credentials_file="$(mktemp)"
runtime_modal_file="$(mktemp)"
credentials_modal_file="$(mktemp)"
trap 'rm -f "${database_url_file}" "${credentials_file}" "${runtime_modal_file}" "${credentials_modal_file}"' EXIT

gcloud secrets versions access latest \
  --secret "${STAGE12_DATABASE_URL_SECRET_NAME}" \
  --project "${STAGE12_GCP_PROJECT_ID}" >"${database_url_file}"

gcloud secrets versions access latest \
  --secret "${STAGE12_GCP_CREDENTIALS_SECRET_NAME}" \
  --project "${STAGE12_GCP_PROJECT_ID}" >"${credentials_file}"

jq -n \
  --rawfile database_url "${database_url_file}" \
  --arg artifact_bucket "${STAGE12_ARTIFACT_BUCKET}" \
  '{STAGE12_DATABASE_URL: ($database_url | rtrimstr("\n")), STAGE12_ARTIFACT_BUCKET: $artifact_bucket}' \
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
