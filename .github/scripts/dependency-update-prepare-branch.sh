#!/usr/bin/env bash

# Rebuild the update branch from current main, and record the commit the
# remote update branch points at, so the push can replace exactly that commit.
#
# Every run starts from main, even when an update PR is open. Merging main into
# an older update branch merges generated uv.lock files line by line, which
# mixes two resolutions into a lock that uv refuses to parse.
#
# Environment:
#   GITHUB_OUTPUT  receives remote_head: the remote update branch's commit, or
#                  empty when the branch does not exist. git reads the remote
#                  with the credentials the checkout stored.

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

branch="update-dependencies"

git config user.name "policyengine-auto"
git config user.email "policyengine-auto@users.noreply.github.com"

remote_head="$(git ls-remote --heads origin "refs/heads/${branch}" | cut -f1)"
if [[ -n "${remote_head}" ]]; then
  # The push step compares the rebuilt branch with this commit.
  git fetch --quiet origin "${remote_head}"
fi

git checkout -B "${branch}" origin/main
echo "remote_head=${remote_head}" >> "${GITHUB_OUTPUT}"
