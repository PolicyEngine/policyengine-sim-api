#!/usr/bin/env bash
# Build the independent Stage 12 cache and export its manifest digest.

set -euo pipefail

modal_environment="${1:?Modal environment is required}"
force="${2:-false}"
: "${STAGE12_CACHE_BUCKET:?STAGE12_CACHE_BUCKET is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

command=(uv run modal run --env="${modal_environment}" src/modal/stage12_precompute_app.py)
if [[ "${force}" == "true" || "${force}" == "1" ]]; then
  command+=(--force)
fi

output_file="$(mktemp)"
trap 'rm -f "${output_file}"' EXIT
"${command[@]}" 2>&1 | tee "${output_file}"
digest_line="$(tr -d '\r' <"${output_file}" | grep '^STAGE12_CACHE_MANIFEST_DIGEST=' | tail -n 1 || true)"
if [[ -z "${digest_line}" ]]; then
  echo "Stage 12 precompute returned no cache manifest digest" >&2
  exit 1
fi
digest="${digest_line#STAGE12_CACHE_MANIFEST_DIGEST=}"
if [[ ! "${digest}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Stage 12 precompute returned an invalid cache manifest digest" >&2
  exit 1
fi
echo "manifest_digest=${digest}" >>"${GITHUB_OUTPUT}"
