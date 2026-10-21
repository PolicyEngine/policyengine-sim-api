#!/usr/bin/env bash
# Select the versioned v2 Modal application name from the packaged
# PolicyEngine.py bundle.
# Run from projects/policyengine-simulation-executor.
# Outputs: name in GITHUB_OUTPUT.

set -euo pipefail

name="$(uv run --frozen python -c 'from policyengine_simulation_contract.stage12_manifest import v2_application_name; from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle; print(v2_application_name(load_stage12_bundle().bundle.policyengine_version))')"
echo "name=${name}" >> "$GITHUB_OUTPUT"
