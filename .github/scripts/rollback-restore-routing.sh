#!/usr/bin/env bash
# Restore the target's Modal routing: undo a deployment run's routing
# changes from the plans it kept, or point latest at a published release.
# With DEPLOY_RUN_ID, every plan that run's attempts kept (not the jobs a rerun
# carried over) must be under $RUNNER_TEMP/routing-plans, downloaded by the
# workflow: each attempt's
# routes plan (routing-plan-<target>-<attempt>, kept by the routes job before
# the checks) and latest plan (routing-latest-plan-<target>-<attempt>, kept by
# the latest job after them). The plans are passed oldest first: by attempt,
# and the routes plan before the latest plan within an attempt. Without it,
# latest is restored to POLICYENGINE_VERSION, refusing unless latest is
# currently EXPECTED_LATEST when that is given.
# Run from projects/policyengine-simulation-executor.
# Required env vars: TARGET, DEPLOY_RUN_ID, POLICYENGINE_VERSION,
# EXPECTED_LATEST, MODAL_ENVIRONMENT, GH_TOKEN (read by gh), and the runner's
# GITHUB_REPOSITORY and RUNNER_TEMP. The Modal client reads MODAL_TOKEN_ID and
# MODAL_TOKEN_SECRET.

set -euo pipefail

if [[ -n "${DEPLOY_RUN_ID}" ]]; then
  # Every plan an attempt's routing jobs kept must be here: restoring
  # without one would stop short of what the run replaced and still look
  # complete.
  attempts="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}" --jq .run_attempt)"
  for kind in routing latest; do
    if [[ "${kind}" == "routing" ]]; then
      job="Publish routing after all services pass"
      step="Keep the plan for a guarded restore"
      prefix="routing-plan-${TARGET}"
    else
      job="Move latest to the release after every check"
      step="Keep the latest plan for a guarded restore"
      prefix="routing-latest-plan-${TARGET}"
    fi
    # A rerun lists each job it carried over from the attempt before, with
    # that job's start time and steps, but uploads nothing for it. A job kept
    # a plan in this attempt only if it started at a time the attempt before
    # does not list.
    previous_starts=""
    for attempt in $(seq 1 "${attempts}"); do
      starts="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}/attempts/${attempt}/jobs" \
        --jq ".jobs[] | select(.name == \"Complete ${TARGET} deployment / ${job}\") | select(any(.steps[]; .name == \"${step}\" and .conclusion == \"success\")) | .started_at" \
        | sort -u)"
      kept=false
      while IFS= read -r start; do
        if [[ -n "${start}" ]] && ! grep -qxF -- "${start}" <<<"${previous_starts}"; then
          kept=true
        fi
      done <<<"${starts}"
      if [[ "${kept}" == "true" && ! -f "${RUNNER_TEMP}/routing-plans/${prefix}-${attempt}/plan.json" ]]; then
        echo "Attempt ${attempt} of run ${DEPLOY_RUN_ID} kept a ${TARGET} ${kind} plan that cannot be found; if its publication is pending, rerun this workflow with target=${TARGET} and resolve_pending_publication=true, leaving other inputs blank." >&2
        exit 1
      fi
      previous_starts="${starts}"
    done
  done
  # Passed oldest first. The restore undoes every change that is still
  # live, newest first, and skips plans that changed nothing.
  plans=()
  for attempt in $(
    for directory in "${RUNNER_TEMP}"/routing-plans/routing-plan-"${TARGET}"-*/ \
      "${RUNNER_TEMP}"/routing-plans/routing-latest-plan-"${TARGET}"-*/; do
      name="${directory%/}"
      name="${name##*-}"
      if [[ "${name}" =~ ^[0-9]+$ ]]; then echo "${name}"; fi
    done | sort -n -u
  ); do
    for artifact in "routing-plan-${TARGET}-${attempt}" "routing-latest-plan-${TARGET}-${attempt}"; do
      if [[ -d "${RUNNER_TEMP}/routing-plans/${artifact}" ]]; then
        plans+=("${RUNNER_TEMP}/routing-plans/${artifact}/plan.json")
      fi
    done
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
