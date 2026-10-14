#!/usr/bin/env bash

# Resolve the URL, revision and image digest of the no-traffic candidate
# revision that carries the given Cloud Run tag.
#
# Required env vars: ENTRYPOINT_SERVICE, TAG, PROJECT_ID, REGION, GITHUB_OUTPUT.
# Outputs: url, revision and image in GITHUB_OUTPUT. Fails when the tag has no
# URL or revision, or the revision's image is not pinned by digest.

set -euo pipefail

service_json="$(gcloud run services describe "${ENTRYPOINT_SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format=json)"
url="$(jq -er --arg tag "${TAG}" '
  .status.traffic[]
  | select(.tag == $tag)
  | .url
  | select(type == "string" and length > 0)
' <<<"${service_json}")"
revision="$(jq -er --arg tag "${TAG}" '
  .status.traffic[]
  | select(.tag == $tag)
  | .revisionName
  | select(type == "string" and length > 0)
' <<<"${service_json}")"
revision_json="$(gcloud run revisions describe "${revision}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format=json)"
image="$(jq -er '
  (.status.imageDigest // .spec.containers[0].image)
  | select(
      type == "string"
      and contains("@sha256:")
    )
' <<<"${revision_json}")"
{
  echo "url=${url}"
  echo "revision=${revision}"
  echo "image=${image}"
} >> "$GITHUB_OUTPUT"
