#!/usr/bin/env bash
# Proof fixture: each secret is resolved in its own assignment, so set -e stops
# the script at the first failure.
set -euo pipefail

runtime_secrets=()

add_runtime_secret() {
  local variable_name="$1"
  local resolved_reference
  resolved_reference="$(resolve_secret_reference "$2")"
  runtime_secrets+=("${variable_name}=${resolved_reference}")
}

add_runtime_secret OLD_GATEWAY_AUTH_CLIENT_SECRET "${OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME}"
add_runtime_secret SIMULATION_DATABASE_URL "${SIMULATION_DATABASE_URL_SECRET_NAME}"
