#!/bin/bash
# Sync secrets from GitHub to Modal environment
# Usage: ./modal-sync-secrets.sh <modal-environment> <gh-environment>
# Required env vars: HF_TOKEN
# Optional env vars: GCP_CREDENTIALS_JSON

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
GH_ENV="${2:?GitHub environment required}"

truthy() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

echo "Syncing secrets to Modal environment: $MODAL_ENV"

if [ -z "${HF_TOKEN:-}" ]; then
  echo "HF_TOKEN is required to sync the Hugging Face dataset secret." >&2
  echo "Add HF_TOKEN to the GitHub environment secrets for '$GH_ENV'." >&2
  exit 1
fi

GATEWAY_AUTH_VARS=(
  GATEWAY_AUTH_ISSUER
  GATEWAY_AUTH_AUDIENCE
  GATEWAY_AUTH_CLIENT_ID
  GATEWAY_AUTH_CLIENT_SECRET
)
present=()
missing=()
for var in "${GATEWAY_AUTH_VARS[@]}"; do
  if [ -n "${!var:-}" ]; then
    present+=("$var")
  else
    missing+=("$var")
  fi
done

if [ ${#present[@]} -gt 0 ] && [ ${#missing[@]} -gt 0 ]; then
  echo "Gateway auth config is partial." >&2
  echo "  Present: ${present[*]-}" >&2
  echo "  Missing: ${missing[*]-}" >&2
  echo "Refusing to sync a broken auth secret state." >&2
  exit 1
fi

if truthy "${GATEWAY_AUTH_REQUIRED:-}" && [ ${#missing[@]} -gt 0 ]; then
  echo "GATEWAY_AUTH_REQUIRED is enabled but gateway auth secrets are missing." >&2
  echo "  Missing: ${missing[*]-}" >&2
  exit 1
fi

# Sync GCP credentials if provided
if [ -n "${GCP_CREDENTIALS_JSON:-}" ]; then
  uv run modal secret create gcp-credentials \
    "GOOGLE_APPLICATION_CREDENTIALS_JSON=$GCP_CREDENTIALS_JSON" \
    --env="$MODAL_ENV" \
    --force || true
fi

# Sync Hugging Face token for private certified datasets used during bundle
# image build and worker runtime.
uv run modal secret create huggingface-token \
  "HF_TOKEN=$HF_TOKEN" \
  --env="$MODAL_ENV" \
  --force

# Sync gateway auth config. The gateway runtime only needs issuer/audience and
# the explicit requirement flag; client credentials stay on the GitHub side and
# are only used to mint integration-test tokens.
NORMALIZED_ISSUER="${GATEWAY_AUTH_ISSUER:-}"
if [ -n "$NORMALIZED_ISSUER" ]; then
  case "$NORMALIZED_ISSUER" in
    */) ;;
    *) NORMALIZED_ISSUER="$NORMALIZED_ISSUER/" ;;
  esac
fi

uv run modal secret create policyengine-gateway-auth \
  "GATEWAY_AUTH_ISSUER=$NORMALIZED_ISSUER" \
  "GATEWAY_AUTH_AUDIENCE=${GATEWAY_AUTH_AUDIENCE:-}" \
  "GATEWAY_AUTH_REQUIRED=${GATEWAY_AUTH_REQUIRED:-}" \
  --env="$MODAL_ENV" \
  --force

echo "Modal secrets synced"
