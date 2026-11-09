#!/usr/bin/env bash
# Read the pinned Public API commit whose v2 migration chain sim's table
# models describe (rule sim.persistence.models-match-public-api).
#
# Usage: public-api-schema-commit.sh <pin file>
#
# The pin file holds one full commit hash. The script prints it and, on
# GitHub Actions, passes it to the later steps as the step output `commit`.
set -euo pipefail

pin_file="${1:?usage: public-api-schema-commit.sh <pin file>}"
commit="$(tr -d '[:space:]' < "${pin_file}")"
if ! [[ "${commit}" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s must hold one Public API commit as 40 lowercase hexadecimal characters\n' \
    "${pin_file}" >&2
  exit 1
fi
printf 'Pinned Public API commit: %s\n' "${commit}"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'commit=%s\n' "${commit}" >> "${GITHUB_OUTPUT}"
fi
