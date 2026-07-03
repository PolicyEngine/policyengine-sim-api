#!/bin/bash
# Deploy simulation API to Modal
# Usage: ./modal-deploy-app.sh <modal-environment> [force-latest]
# Required env vars: POLICYENGINE_VERSION, POLICYENGINE_CORE_VERSION,
# POLICYENGINE_US_VERSION, POLICYENGINE_UK_VERSION
# These should come from the bundled policyengine.py release manifest.
#
# Deploys two apps:
# 1. policyengine-simulation-gateway - Stable gateway with fixed URL
# 2. policyengine-simulation-py{X} - Versioned simulation app

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
FORCE_LATEST="${2:-false}"

# Generate versioned simulation app name (dots replaced with dashes for URL safety)
POLICYENGINE_VERSION_SAFE="${POLICYENGINE_VERSION//./-}"
SIMULATION_APP_NAME="policyengine-simulation-py${POLICYENGINE_VERSION_SAFE}"
UPDATE_REGISTRY_COMMAND=(
    uv run python -m src.modal.utils.update_version_registry
    --app-name "$SIMULATION_APP_NAME"
    --policyengine-version "${POLICYENGINE_VERSION}"
    --us-version "${POLICYENGINE_US_VERSION}"
    --uk-version "${POLICYENGINE_UK_VERSION}"
    --environment "$MODAL_ENV"
)
if [[ "$FORCE_LATEST" == "true" || "$FORCE_LATEST" == "1" ]]; then
    UPDATE_REGISTRY_COMMAND+=(--force-latest)
fi

echo "========================================"
echo "Deploying to Modal environment: $MODAL_ENV"
echo "  policyengine.py version: ${POLICYENGINE_VERSION}"
echo "  policyengine-core version: ${POLICYENGINE_CORE_VERSION}"
echo "  US version: ${POLICYENGINE_US_VERSION}"
echo "  UK version: ${POLICYENGINE_UK_VERSION}"
echo "  Force latest: ${FORCE_LATEST}"
echo "========================================"

# 1. Deploy the gateway app (stable URL) from its own project
echo ""
echo "Step 1: Deploying gateway app..."
echo "  App name: policyengine-simulation-gateway"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
(
    cd "$REPO_ROOT/projects/policyengine-simulation-gateway"
    uv run modal deploy --env="$MODAL_ENV" \
        src/policyengine_simulation_gateway/app.py
)

# 2. Deploy the versioned simulation app
echo ""
echo "Step 2: Deploying versioned simulation app..."
echo "  App name: ${SIMULATION_APP_NAME}"
export MODAL_APP_NAME="$SIMULATION_APP_NAME"
uv run modal deploy --env="$MODAL_ENV" src/modal/app.py

# 3. Publish active routing state
echo ""
echo "Step 3: Publishing active routing state..."
"${UPDATE_REGISTRY_COMMAND[@]}"

echo ""
echo "========================================"
echo "Deployment complete!"
echo "  Gateway app: policyengine-simulation-gateway"
echo "  Simulation app: $SIMULATION_APP_NAME"
echo "  Routing state: simulation-api-routing-state[active]"
echo "========================================"
