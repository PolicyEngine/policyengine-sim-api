#!/usr/bin/env bash

# Record the stable Cloud Run service URL and the one revision serving 100
# percent of its traffic, before a candidate is deployed.
#
# Required env vars: ENTRYPOINT_SERVICE, PROJECT_ID, REGION, GITHUB_OUTPUT.
# Outputs: stable_url and revision in GITHUB_OUTPUT. Fails when the service has
# no URL or not exactly one revision at 100 percent.

set -euo pipefail

service_json="$(gcloud run services describe "${ENTRYPOINT_SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format=json)"
stable_url="$(jq -er '.status.url | select(type == "string" and length > 0)' <<<"${service_json}")"
revision="$(jq -er '
  [
    .status.traffic[]?
    | select((.percent // 0) == 100)
    | .revisionName
    | select(type == "string" and length > 0)
  ]
  | if length == 1 then .[0]
    else error("service must have exactly one revision at 100 percent")
    end
' <<<"${service_json}")"
{
  echo "stable_url=${stable_url}"
  echo "revision=${revision}"
} >> "$GITHUB_OUTPUT"
