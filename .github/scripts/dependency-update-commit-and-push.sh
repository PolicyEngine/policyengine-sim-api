#!/usr/bin/env bash

# Commit the regenerated clients and upgraded locks on the update branch, which
# the prepare step rebuilt from main, and replace the remote update branch.
#
# - When the update changes no file on main, nothing is committed or pushed.
# - Otherwise the commit also adds changelog.d/update-dependencies.changed.md,
#   since every pull request adds a changelog fragment (rule
#   sim.process.changelog-fragment). Its name and text never change, so the
#   comparison below still compares only the update.
# - When the remote branch already holds the same files, the push is skipped.
#   The commit message carries the date, so commits are compared by their files.
# - Otherwise the push replaces the remote branch only while it still points at
#   the commit the prepare step recorded (--force-with-lease), so a push made
#   during the run is not lost silently. Without a remote branch, a plain push
#   creates it.
#
# Environment:
#   GITHUB_OUTPUT  receives changes_detected (true when the update changed files
#                  on main, so an update PR should exist) and branch_pushed
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

changes_detected=false
branch_pushed=false
if [[ -z "$(git status --porcelain)" ]]; then
  echo "No changes detected"
else
  echo "Changes detected"
  changes_detected=true
  mkdir -p changelog.d
  echo "Updated every project's locked dependencies to the newest versions their requirements allow." \
    > changelog.d/update-dependencies.changed.md
  git add --all
  git commit -m "Update dependencies ($(date -u +%Y-%m-%d))"

  if [[ -n "${remote_head}" ]] && git diff --quiet "${remote_head}" HEAD --; then
    echo "${branch} already holds this update; not pushing."
  elif [[ -n "${remote_head}" ]]; then
    git push \
      --force-with-lease="refs/heads/${branch}:${remote_head}" \
      origin "HEAD:refs/heads/${branch}"
    branch_pushed=true
  else
    git push origin "HEAD:refs/heads/${branch}"
    branch_pushed=true
  fi
fi

{
  echo "changes_detected=${changes_detected}"
  echo "branch_pushed=${branch_pushed}"
} >> "${GITHUB_OUTPUT}"
