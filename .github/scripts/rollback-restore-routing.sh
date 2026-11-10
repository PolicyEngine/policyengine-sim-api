#!/usr/bin/env bash
# Restore the target's Modal routing: undo a deployment run's routing
# changes from the plans it kept, or point latest at a published release.
# With DEPLOY_RUN_ID, the workflow downloads the plans that run kept into
# $RUNNER_TEMP/routing-plans: an attempt's routes plan
# (routing-plan-<target>-<attempt>, kept by the routes job before the checks)
# and its latest plan (routing-latest-plan-<target>-<attempt>, kept by the
# latest job after them), for each attempt whose job ran rather than being
# carried over by a rerun. A missing plan is refused unless restoring without
# it is still exact (see below). The plans are passed oldest first: by
# attempt, and the routes plan before the latest plan within an attempt.
# Without DEPLOY_RUN_ID, latest is restored to POLICYENGINE_VERSION, refusing
# unless latest is currently EXPECTED_LATEST when that is given.
# Run from projects/policyengine-simulation-executor.
# Required env vars: TARGET, DEPLOY_RUN_ID, POLICYENGINE_VERSION,
# EXPECTED_LATEST, MODAL_ENVIRONMENT, GH_TOKEN (read by gh), and the runner's
# GITHUB_REPOSITORY and RUNNER_TEMP. The Modal client reads MODAL_TOKEN_ID and
# MODAL_TOKEN_SECRET. Reads plan digests with jq.

set -euo pipefail

if [[ -n "${DEPLOY_RUN_ID}" ]]; then
  # Every plan an attempt's routing jobs kept must be here: restoring
  # without one would stop short of what the run replaced and still look
  # complete.
  attempts="$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}" --jq .run_attempt)"
  # The plans the run's jobs kept, in the order they were published: by
  # attempt, and the routes plan before the latest plan within an attempt.
  # A rerun lists each job it carried over from the attempt before, with
  # that job's start time and steps, but uploads nothing for it, so a job
  # kept a plan in an attempt only if it started at a time the attempt
  # before does not list.
  kept_plans=()
  previous_routing_starts=""
  previous_latest_starts=""
  for attempt in $(seq 1 "${attempts}"); do
    for kind in routing latest; do
      if [[ "${kind}" == "routing" ]]; then
        job="Publish routing after all services pass"
        step="Keep the plan for a guarded restore"
        artifact="routing-plan-${TARGET}-${attempt}"
        previous_starts="${previous_routing_starts}"
      else
        job="Move latest to the release after every check"
        step="Keep the latest plan for a guarded restore"
        artifact="routing-latest-plan-${TARGET}-${attempt}"
        previous_starts="${previous_latest_starts}"
      fi
      starts="$(gh api --paginate "repos/${GITHUB_REPOSITORY}/actions/runs/${DEPLOY_RUN_ID}/attempts/${attempt}/jobs" \
        --jq ".jobs[] | select(.name == \"Complete ${TARGET} deployment / ${job}\") | select(any(.steps[]; .name == \"${step}\" and .conclusion == \"success\")) | .started_at" \
        | sort -u)"
      while IFS= read -r start; do
        if [[ -n "${start}" ]] && ! grep -qxF -- "${start}" <<<"${previous_starts}"; then
          kept_plans+=("${artifact}")
          break
        fi
      done <<<"${starts}"
      if [[ "${kind}" == "routing" ]]; then
        previous_routing_starts="${starts}"
      else
        previous_latest_starts="${starts}"
      fi
    done
  done
  # GitHub stops listing an attempt's artifact once the job that uploaded it
  # reruns. A kept plan may be missing only when restoring the downloaded
  # plans without it is still exact:
  # - no downloaded plan was published after it: if its change, or anything
  #   built on it, is live, the live state matches no downloaded plan and the
  #   restore refuses as a conflict, changing nothing; or
  # - the nearest downloaded plans published before and after it show its
  #   change undone: the later plan replaced exactly the state the earlier
  #   one published, and each published state is unique.
  # Without any other missing plan, the restore could stop short of what the
  # run replaced and still look complete, so the rollback refuses.
  for ((index = 0; index < ${#kept_plans[@]}; index++)); do
    artifact="${kept_plans[index]}"
    if [[ -f "${RUNNER_TEMP}/routing-plans/${artifact}/plan.json" ]]; then
      continue
    fi
    earlier_after=""
    for ((other = index - 1; other >= 0; other--)); do
      plan="${RUNNER_TEMP}/routing-plans/${kept_plans[other]}/plan.json"
      if [[ -f "${plan}" ]]; then
        earlier_after="$(jq -r '.after_sha256 // ""' "${plan}")"
        break
      fi
    done
    later_found=false
    later_before=""
    for ((other = index + 1; other < ${#kept_plans[@]}; other++)); do
      plan="${RUNNER_TEMP}/routing-plans/${kept_plans[other]}/plan.json"
      if [[ -f "${plan}" ]]; then
        later_found=true
        later_before="$(jq -r '.before_sha256 // ""' "${plan}")"
        break
      fi
    done
    if [[ "${later_found}" == "false" ]]; then
      continue
    fi
    if [[ "${earlier_after}" =~ ^[0-9a-f]{64}$ && "${later_before}" == "${earlier_after}" ]]; then
      continue
    fi
    if [[ "${artifact}" == routing-latest-plan-* ]]; then kind=latest; else kind=routing; fi
    echo "Attempt ${artifact##*-} of run ${DEPLOY_RUN_ID} kept a ${TARGET} ${kind} plan that cannot be found, and the plans around it do not show its change undone. If its publication is pending, rerun this workflow with target=${TARGET} and resolve_pending_publication=true, leaving other inputs blank; otherwise roll latest back with policyengine_version." >&2
    exit 1
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
