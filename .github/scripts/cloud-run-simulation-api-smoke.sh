#!/usr/bin/env bash

set -euo pipefail

base_url="${1:?Simulation API base URL is required}"
base_url="${base_url%/}"

curl --fail --silent --show-error "${base_url}/health" |
  jq -e '.status == "healthy"' >/dev/null
curl --fail --silent --show-error "${base_url}/ready" |
  jq -e '.status == "ready"' >/dev/null
curl --fail --silent --show-error "${base_url}/versions" >/dev/null
curl --fail --silent --show-error \
  --request POST \
  --header "Content-Type: application/json" \
  --data '{"value": 1}' \
  "${base_url}/ping" |
  jq -e '.incremented == 2' >/dev/null
