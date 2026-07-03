#!/bin/bash
# Run the pre-merge image smokes against a Modal environment.
# Usage: ./modal-image-smoke.sh <modal-environment>
#
# Each smoke imports the true app entrypoints inside the real (or, for
# the executor, prefix-identical) Modal image — catching in-image
# dependency breakage (issue #602's class) before merge.

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "=== Gateway image smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-gateway"
    uv run modal run --env="$MODAL_ENV" \
        src/policyengine_simulation_gateway/smoke_app.py
)

echo "=== Executor image smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run modal run --env="$MODAL_ENV" src/modal/smoke_app.py
)

echo "=== Image smokes passed ==="
