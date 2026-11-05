#!/usr/bin/env bash
# Record the stable Cloud Run service's URL and the revision serving all of
# its traffic.
# Required env vars: SERVICE, PROJECT_ID, REGION, and the runner's
# GITHUB_OUTPUT. gcloud uses the job's Google credentials.
# Outputs: url and revision in GITHUB_OUTPUT.
#
# As in the inline step this replaced, a jq failure inside an echo argument
# does not stop the script: without a URL it records url=null, and without
# exactly one revision at 100 percent it records an empty revision. A gcloud
# failure does stop it.

set -euo pipefail

service_json="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" --region "${REGION}" --format=json)"
echo "url=$(jq -er '.status.url' <<<"${service_json}")" >> "$GITHUB_OUTPUT"
echo "revision=$(jq -er '
  [.status.traffic[]? | select((.percent // 0) == 100) | .revisionName]
  | if length == 1 then .[0] else error("no single 100 percent revision") end
' <<<"${service_json}")" >> "$GITHUB_OUTPUT"
