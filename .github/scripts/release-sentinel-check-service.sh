#!/usr/bin/env bash
# Check that a deployed simulation service reproduces the release sentinel's
# numbers.
# Usage: release-sentinel-check-service.sh <base-url>
# Run from projects/policyengine-simulation-executor.
# Required env vars: the runner's RUNNER_TEMP. The sentinel check
# authenticates with SIMULATION_TEST_AUTH_ISSUER,
# SIMULATION_TEST_AUTH_AUDIENCE, SIMULATION_TEST_AUTH_CLIENT_ID and
# SIMULATION_TEST_AUTH_CLIENT_SECRET when they are set.
# Reads: $RUNNER_TEMP/sentinel/release-sentinel.json.

set -euo pipefail

base_url="${1?usage: release-sentinel-check-service.sh <base-url>}"

uv sync --frozen
uv run --frozen python -m src.modal.utils.release_sentinel check \
  --base-url "${base_url}" \
  --expected "${RUNNER_TEMP}/sentinel/release-sentinel.json"
