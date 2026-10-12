#!/usr/bin/env bash

# Validate simulation-rollback.yml inputs before anything is changed. Inputs
# arrive through the environment, never interpolated into shell source.

set -euo pipefail

version_pattern='^[0-9]+(\.[0-9]+){1,3}([a-z0-9.+-]*)?$'
revision_pattern='^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$'
run_id_pattern='^[1-9][0-9]{0,19}$'

fail() {
  printf 'Rollback input error: %s\n' "$1" >&2
  exit 2
}

case "${TARGET:-}" in
  beta|prod) ;;
  *) fail "TARGET must be beta or prod" ;;
esac

if [[ -n "${DEPLOY_RUN_ID:-}" && -n "${POLICYENGINE_VERSION:-}" ]]; then
  fail "give deploy_run_id or policyengine_version, not both"
fi
if [[ -n "${DEPLOY_RUN_ID:-}" ]]; then
  [[ "${DEPLOY_RUN_ID}" =~ ${run_id_pattern} ]] \
    || fail "deploy_run_id is not a workflow run id"
  [[ -z "${EXPECTED_LATEST:-}" ]] \
    || fail "expected_latest applies only with policyengine_version"
elif [[ -n "${POLICYENGINE_VERSION:-}" ]]; then
  [[ "${POLICYENGINE_VERSION}" =~ ${version_pattern} ]] \
    || fail "policyengine_version is not a version"
  if [[ -n "${EXPECTED_LATEST:-}" ]]; then
    [[ "${EXPECTED_LATEST}" =~ ${version_pattern} ]] \
      || fail "expected_latest is not a version"
  fi
else
  fail "give deploy_run_id or policyengine_version"
fi
if [[ -n "${ENTRYPOINT_REVISION:-}" ]]; then
  [[ "${ENTRYPOINT_REVISION}" =~ ${revision_pattern} ]] \
    || fail "entrypoint_revision is not a Cloud Run revision name"
fi

if [[ -n "${DEPLOY_RUN_ID:-}" ]]; then
  printf 'Undoing the routing change of %s deployment run %s' "${TARGET}" "${DEPLOY_RUN_ID}"
else
  printf 'Rolling %s back to policyengine %s' "${TARGET}" "${POLICYENGINE_VERSION}"
fi
[[ -n "${ENTRYPOINT_REVISION:-}" ]] && printf ', entrypoint %s' "${ENTRYPOINT_REVISION}"
printf '.\n'
