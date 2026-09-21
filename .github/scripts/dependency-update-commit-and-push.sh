#!/usr/bin/env bash

# Commit generated dependency changes and update the reusable PR branch.

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${HAS_OPEN_PR:?HAS_OPEN_PR is required}"

if [[ "${HAS_OPEN_PR}" != "true" && "${HAS_OPEN_PR}" != "false" ]]; then
  echo "HAS_OPEN_PR must be either true or false" >&2
  exit 1
fi

changes_detected=false
if [[ -z "$(git status --porcelain)" ]]; then
  echo "No changes detected"
else
  echo "Changes detected"
  git add --all
  git commit -m "Update dependencies ($(date -u +%Y-%m-%d))"
  changes_detected=true
fi

branch_pushed=false
if [[ "${HAS_OPEN_PR}" == "true" ]]; then
  commits_to_push="$(git rev-list --count origin/update-dependencies..HEAD)"
  if [[ "${commits_to_push}" -gt 0 ]]; then
    git push origin HEAD:update-dependencies
    branch_pushed=true
  fi
elif [[ "${changes_detected}" == "true" ]]; then
  # No open PR owns this branch, so replacing a stale remote branch cannot
  # rewrite active review history.
  git push --force-with-lease origin HEAD:update-dependencies
  branch_pushed=true
fi

echo "branch_pushed=${branch_pushed}" >> "${GITHUB_OUTPUT}"
