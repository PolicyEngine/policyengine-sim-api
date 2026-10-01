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

if [ -z "${HF_TOKEN:-}" ]; then
    echo "HF_TOKEN is required to prepare the Hugging Face dataset secret." >&2
    exit 1
fi

# Keep the pre-merge image check independent of the last deployment's secret
# schema. GitHub stores this credential as HF_TOKEN; PolicyEngine workers read
# it from HUGGING_FACE_TOKEN inside Modal.
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run modal secret create huggingface-token \
        "HUGGING_FACE_TOKEN=$HF_TOKEN" \
        --env="$MODAL_ENV" \
        --force
)

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

echo "=== Stage 12 static runtime file smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run modal run --env="$MODAL_ENV" \
        src/modal/v2_static_runtime_files_smoke.py
)

echo "=== Image smokes passed ==="
