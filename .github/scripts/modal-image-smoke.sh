#!/bin/bash
# Run the pre-merge image smokes against a Modal environment.
# Usage: ./modal-image-smoke.sh <modal-environment>
#
# Each smoke imports the true app entrypoints inside the real (or, for
# the executor and the v2 US worker, prefix-identical) Modal image —
# catching in-image dependency breakage (issue #602's class) before merge.
# The v2 image smoke builds every image of the v2 application: the report
# coordinator, the country simulation workers and the household functions.

set -euo pipefail

MODAL_ENV="${1:?Modal environment required}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ -z "${PE_UK_PRIVATE_HF_READ_TOKEN:-}" ]; then
    echo "PE_UK_PRIVATE_HF_READ_TOKEN is required to prepare the UK private-data Hugging Face credential." >&2
    exit 1
fi

# Keep the pre-merge image check independent of the last deployment's secret
# schema. GitHub stores this credential under its purpose-specific name;
# PolicyEngine Core reads the HUGGING_FACE_TOKEN compatibility name in Modal.
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run --frozen modal secret create pe-uk-private-hf-read-token \
        "HUGGING_FACE_TOKEN=$PE_UK_PRIVATE_HF_READ_TOKEN" \
        --env="$MODAL_ENV" \
        --force
)

echo "=== Gateway image smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-gateway"
    uv run --frozen modal run --env="$MODAL_ENV" \
        src/policyengine_simulation_gateway/smoke_app.py
)

echo "=== Executor image smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run --frozen modal run --env="$MODAL_ENV" src/modal/smoke_app.py
)

echo "=== Stage 12 static runtime file smoke (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run --frozen modal run --env="$MODAL_ENV" \
        src/modal/v2_static_runtime_files_smoke.py
)

echo "=== v2 image smoke: every v2 image and its entry points (env: $MODAL_ENV) ==="
(
    cd "$REPO_ROOT/projects/policyengine-simulation-executor"
    uv run --frozen modal run --env="$MODAL_ENV" src/modal/v2_image_smoke.py
)

echo "=== Image smokes passed ==="
