#!/usr/bin/env bash

# Reuse the branch for an open dependency-update PR, or start a new branch
# from current main when no update PR exists.

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

git config user.name "policyengine-auto"
git config user.email "policyengine-auto@users.noreply.github.com"

open_pr="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --base main \
    --head update-dependencies \
    --state open \
    --json number \
    --jq '.[0].number // empty'
)"

if [[ -n "${open_pr}" ]]; then
  echo "has_open_pr=true" >> "${GITHUB_OUTPUT}"
  git fetch origin update-dependencies
  git checkout -B update-dependencies origin/update-dependencies

  # Preserve the open PR's commits while incorporating current main. Prefer
  # main's version of conflicts because later steps regenerate clients and
  # recreate dependency lock changes.
  git merge --no-edit -X theirs origin/main
else
  echo "has_open_pr=false" >> "${GITHUB_OUTPUT}"
  git checkout -B update-dependencies origin/main
fi
