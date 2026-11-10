#!/usr/bin/env bash
# Finish this run's routing publication in the durable routing journal:
# complete it when every release check passed, otherwise restore what it
# replaced. Caller workflows serialize this write with every other routing
# write in the simulation-deploy-main concurrency group.
# Run from projects/policyengine-simulation-executor.
# Required env vars: MODAL_ENVIRONMENT, PUBLICATION_RUN (the deployment's run
# ID), RELEASE_ENVIRONMENT, GATES_PASSED ("true" or "false"), and the
# runner's GITHUB_OUTPUT. The Modal client reads MODAL_TOKEN_ID and
# MODAL_TOKEN_SECRET.
# Outputs: outcome in GITHUB_OUTPUT, written by the publisher.

set -euo pipefail

uv run --frozen python -m src.modal.utils.update_version_registry \
  --environment "${MODAL_ENVIRONMENT}" \
  --finalize-publication --publication-run "${PUBLICATION_RUN}" \
  --release-environment "${RELEASE_ENVIRONMENT}" \
  --gates-passed "${GATES_PASSED}" --github-output "$GITHUB_OUTPUT"
