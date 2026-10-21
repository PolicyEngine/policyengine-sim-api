#!/usr/bin/env bash

# Commit the regenerated clients and upgraded locks on the update branch, which
# the prepare step rebuilt from main, and replace the remote update branch.
#
# The push replaces the remote branch only while it still points at the commit
# the prepare step recorded (--force-with-lease), so a push made during the run
# is not lost silently. Without a remote branch, a plain push creates it.
#
# Environment:
#   GITHUB_OUTPUT  receives branch_pushed=true or branch_pushed=false
#   REMOTE_HEAD    the prepare step's remote_head output: the remote update
#                  branch's commit, or empty when the branch does not exist

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

branch="update-dependencies"
remote_head="${REMOTE_HEAD:-}"
if [[ -n "${remote_head}" && ! "${remote_head}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "REMOTE_HEAD must be empty or a full commit hash: ${remote_head}" >&2
  exit 1
fi

branch_pushed=false
if [[ -z "$(git status --porcelain)" ]]; then
  echo "No changes detected"
else
  echo "Changes detected"
  git add --all
  git commit -m "Update dependencies ($(date -u +%Y-%m-%d))"

  if [[ -n "${remote_head}" ]]; then
    git push \
      --force-with-lease="refs/heads/${branch}:${remote_head}" \
      origin "HEAD:refs/heads/${branch}"
  else
    git push origin "HEAD:refs/heads/${branch}"
  fi
  branch_pushed=true
fi

echo "branch_pushed=${branch_pushed}" >> "${GITHUB_OUTPUT}"
