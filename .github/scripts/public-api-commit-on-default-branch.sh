#!/usr/bin/env bash
# Require the checked-out Public API commit to be on the Public API's default
# branch, so sim's table models only ever describe a migration chain the
# Public API has merged (rule sim.persistence.models-match-public-api).
#
# Usage: public-api-commit-on-default-branch.sh <Public API checkout> <branch>
set -euo pipefail

checkout="${1:?usage: public-api-commit-on-default-branch.sh <checkout> <branch>}"
branch="${2:?usage: public-api-commit-on-default-branch.sh <checkout> <branch>}"
commit="$(git -C "${checkout}" rev-parse HEAD)"
git -C "${checkout}" fetch --quiet origin "${branch}"
if ! git -C "${checkout}" merge-base --is-ancestor "${commit}" FETCH_HEAD; then
  printf '%s is not on the Public API'"'"'s %s branch yet; pin a merged commit\n' \
    "${commit}" "${branch}" >&2
  exit 1
fi
printf '%s is on the Public API'"'"'s %s branch\n' "${commit}" "${branch}"
