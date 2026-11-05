#!/usr/bin/env bash

# Require a changelog fragment in a pull request (rule
# sim.process.changelog-fragment). The "Changelog fragment" job in
# changelog-fragment.yml runs this on every pull request, on the merged result
# that GitHub checks out.
#
# It lists the files the pull request adds relative to its base and passes
# them to changelog_fragments.py, which fails unless one of them is a fragment
# and every file in changelog.d/ is one. A rename counts as a removal and an
# addition, so moving a misplaced file into place counts. Fragments already on
# the base branch belong to the pull requests that merged them, and don't
# count. In a stack, the base branch is the branch of the pull request below,
# so its fragment doesn't count for the one above.
#
# Run from the repository root.
#
# Environment:
#   BASE_SHA  the commit the pull request merges into, the tip of its own
#             base branch (github.event.pull_request.base.sha); locally, for
#             example, BASE_SHA=origin/main

set -euo pipefail

base_revision="${BASE_SHA:?BASE_SHA is required: the commit the pull request merges into, such as origin/main}"
script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! merge_base="$(git merge-base "${base_revision}" HEAD)"; then
  echo "::error::Could not find the merge base of ${base_revision} and HEAD; the checkout needs the base branch's history (fetch-depth: 0)."
  exit 1
fi

git diff --no-renames --diff-filter=A --name-only -z "${merge_base}" HEAD \
  | python3 "${script_directory}/changelog_fragments.py" check-pull-request
