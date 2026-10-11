#!/usr/bin/env bash

# Validate simulation-rollback.yml inputs before anything is changed. Inputs
# arrive through the environment, never interpolated into shell source.

set -euo pipefail

version_pattern='^[0-9]+(\.[0-9]+){1,3}([a-z0-9.+-]*)?$'
revision_pattern='^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$'

fail() {
  printf 'Rollback input error: %s\n' "$1" >&2
  exit 2
}

case "${TARGET:-}" in
  beta|prod) ;;
  *) fail "TARGET must be beta or prod" ;;
esac

[[ "${POLICYENGINE_VERSION:-}" =~ ${version_pattern} ]] \
  || fail "policyengine_version is not a version"
if [[ -n "${EXPECTED_LATEST:-}" ]]; then
  [[ "${EXPECTED_LATEST}" =~ ${version_pattern} ]] \
    || fail "expected_latest is not a version"
fi
if [[ -n "${WITHDRAW_VERSION:-}" ]]; then
  [[ "${WITHDRAW_VERSION}" =~ ${version_pattern} ]] \
    || fail "withdraw_version is not a version"
  [[ "${WITHDRAW_VERSION}" != "${POLICYENGINE_VERSION}" ]] \
    || fail "cannot restore and withdraw the same version"
fi
if [[ -n "${ENTRYPOINT_REVISION:-}" ]]; then
  [[ "${ENTRYPOINT_REVISION}" =~ ${revision_pattern} ]] \
    || fail "entrypoint_revision is not a Cloud Run revision name"
fi

printf 'Rolling %s back to policyengine %s' "${TARGET}" "${POLICYENGINE_VERSION}"
[[ -n "${WITHDRAW_VERSION:-}" ]] && printf ', withdrawing %s' "${WITHDRAW_VERSION}"
[[ -n "${ENTRYPOINT_REVISION:-}" ]] && printf ', entrypoint %s' "${ENTRYPOINT_REVISION}"
printf '.\n'
