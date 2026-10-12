#!/usr/bin/env bash
# Version and build the generated simulation API Python client.
# Usage: build-simulation-api-client.sh <client-directory>
# Required env vars: RUN_NUMBER (the workflow run number).
# The client is versioned 0.<YYYYMMDD>.<RUN_NUMBER> and built into
# <client-directory>/dist/.

set -euo pipefail

CLIENT_DIR="${1:?Client directory required}"
: "${RUN_NUMBER:?RUN_NUMBER is required}"

cd "$CLIENT_DIR"
# Update version
DATE=$(date +%Y%m%d)
NEW_VERSION="0.${DATE}.${RUN_NUMBER}"
sed -i.bak "s/^version = .*/version = \"${NEW_VERSION}\"/" pyproject.toml
rm pyproject.toml.bak
uv build
