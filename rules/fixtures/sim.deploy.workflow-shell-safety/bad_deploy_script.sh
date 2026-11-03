#!/usr/bin/env bash
# Proof fixture: one array assignment resolves two secrets, so a failure to
# resolve the first is ignored and the deploy goes on with an empty reference.
set -euo pipefail

runtime_secrets=(
  "OLD_GATEWAY_AUTH_CLIENT_SECRET=$(resolve_secret_reference "${OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME}")"
  "SIMULATION_DATABASE_URL=$(resolve_secret_reference "${SIMULATION_DATABASE_URL_SECRET_NAME}")"
)
