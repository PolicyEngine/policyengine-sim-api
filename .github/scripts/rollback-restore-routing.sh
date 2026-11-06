#!/usr/bin/env bash
# Restore the target's Modal routing: undo a deployment run's routing
# changes from the plans it kept, or point latest at a published release.
# With DEPLOY_RUN_ID, every attempt of that run whose routing job kept a plan
# must have its plan under $RUNNER_TEMP/routing-plans, downloaded by the
# workflow; the plans are passed oldest first. Without it, latest is
# restored to POLICYENGINE_VERSION, refusing unless latest is currently
# EXPECTED_LATEST when that is given.
# Run from projects/policyengine-simulation-executor.
# Required env vars: TARGET, DEPLOY_RUN_ID, POLICYENGINE_VERSION,
# EXPECTED_LATEST, MODAL_ENVIRONMENT, GH_TOKEN (read by gh), and the runner's
# GITHUB_REPOSITORY and RUNNER_TEMP. The Modal client reads MODAL_TOKEN_ID and
# MODAL_TOKEN_SECRET.

set -euo pipefail

if [[ -n "${DEPLOY_RUN_ID}" ]]; then
  # Every attempt whose routing job kept a plan must have one here:
  # restoring without an attempt's plan would stop short of what
  # the run replaced and still look complete.
  attempts="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}" --jq .run_attempt)"
  for attempt in $(seq 1 "${attempts}"); do
    kept="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}/attempts/${attempt}/jobs" \
      --jq "[.jobs[] | select(.name == \"Complete ${TARGET} deployment / Publish routing after all services pass\") | .steps[] | select(.name == \"Keep the plan for a guarded restore\" and .conclusion == \"success\")] | length" \
      | awk '{total += $1} END {print total + 0}')"
    if [[ "${kept}" -gt 0 && ! -f "${RUNNER_TEMP}/routing-plans/routing-plan-${TARGET}-${attempt}/plan.json" ]]; then
      echo "Attempt ${attempt} of run ${DEPLOY_RUN_ID} kept a ${TARGET} routing plan that cannot be found; if its publication is pending, rerun this workflow with target=${TARGET} and resolve_pending_publication=true, leaving other inputs blank." >&2
      exit 1
    fi
  done
  # One plan per run attempt, passed oldest first. The restore undoes
  # every attempt's change that is still live, newest first, and skips
  # attempts that changed nothing.
  plans=()
  for attempt in $(
    for directory in "${RUNNER_TEMP}"/routing-plans/routing-plan-"${TARGET}"-*/; do
      name="${directory%/}"
      name="${name##*-}"
      if [[ "${name}" =~ ^[0-9]+$ ]]; then echo "${name}"; fi
    done | sort -n
  ); do
    plans+=("${RUNNER_TEMP}/routing-plans/routing-plan-${TARGET}-${attempt}/plan.json")
  done
  if [[ ${#plans[@]} -eq 0 ]]; then
    echo "Deployment run ${DEPLOY_RUN_ID} kept no ${TARGET} routing plan." >&2
    exit 1
  fi
  uv run --frozen python -m src.modal.utils.update_version_registry \
    --environment "${MODAL_ENVIRONMENT}" \
    --restore-plan "${plans[@]}"
else
  args=(--environment "${MODAL_ENVIRONMENT}" --restore-latest "${POLICYENGINE_VERSION}")
  if [[ -n "${EXPECTED_LATEST}" ]]; then
    args+=(--expected-latest "${EXPECTED_LATEST}")
  fi
  uv run --frozen python -m src.modal.utils.update_version_registry "${args[@]}"
fi
