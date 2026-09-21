#!/usr/bin/env bash

# Open a dependency-update PR after a new update branch has been pushed.

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

gh pr create \
  --repo "${GITHUB_REPOSITORY}" \
  --title "Update dependencies" \
  --body "Automated daily dependency updates. Subsequent runs append commits to this branch while the pull request remains open." \
  --base main \
  --head update-dependencies
