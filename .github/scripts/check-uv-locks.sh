#!/usr/bin/env bash
# Check that every project's uv.lock matches its pyproject.toml.
#
# The projects are the directories of the tracked pyproject.toml files (test
# fixtures excluded), so a new project is checked without editing this script
# or the workflow. Every project is checked before the script fails, so one run
# reports every stale lock. See docs/engineering/skills/dependencies.md.
set -euo pipefail

failed=0
while IFS= read -r pyproject; do
  project="$(dirname "${pyproject}")"
  echo "Checking ${project}/uv.lock"
  if ! uv lock --check --project "${project}"; then
    echo "::error file=${project}/uv.lock::uv.lock does not match pyproject.toml; run uv lock --project ${project}" >&2
    failed=1
  fi
done < <(git ls-files -- '*pyproject.toml' ':!:**/fixtures/**')

exit "${failed}"
