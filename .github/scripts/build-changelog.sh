#!/usr/bin/env bash

# Build the next release: raise VERSION from the pending changelog fragments,
# then let Towncrier write their section of CHANGELOG.md and remove them with
# git rm. The versioning job in simulation-deploy.yml runs this after a merge
# to main and commits the result with commit-release.sh; make changelog runs
# it locally.
#
#   build-changelog.sh          build the release
#   build-changelog.sh --draft  print the section the next release would add,
#                               and change no file
#
# changelog_fragments.py refuses to bump unless changelog.d/ holds at least
# one fragment and nothing else, so Towncrier never skips a file. Towncrier
# runs from the locked environment in .github/towncrier (its uv.lock).
#
# Run from the repository root, with Python 3.11 or later as python3.

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fragments="${script_directory}/changelog_fragments.py"

case "${1:-}" in
  "")
    python3 "${fragments}" bump
    read -r version < VERSION
    uv run --frozen --project .github/towncrier towncrier build --yes --version "${version}"
    ;;
  --draft)
    next_version="$(python3 "${fragments}" next-version)"
    uv run --frozen --project .github/towncrier towncrier build --draft --version "${next_version}"
    ;;
  *)
    echo "Usage: build-changelog.sh [--draft]" >&2
    exit 2
    ;;
esac
