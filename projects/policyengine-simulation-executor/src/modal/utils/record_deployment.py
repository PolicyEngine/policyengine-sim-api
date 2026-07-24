"""Record a deployed environment's manifest in the artifact store.

Runs on the deploy runner after the health check, writing the
``deployed/<environment>.json`` marker (last-writer-wins). The marker is
the artifact GC's liveness signal, so a deploy that cannot record itself
must fail the job even though Modal already serves it.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Mapping

from policyengine_simulation_executor.artifact_store import ArtifactStore
from policyengine_simulation_executor.precompute_models import DeployedMarker


def build_marker(
    environment: str,
    manifest_digest: str,
    env: Mapping[str, str],
) -> DeployedMarker:
    """Pure marker assembly: the deploy identity that referenced this
    manifest. Missing env vars become empty fields, never errors — the
    marker write itself is the part that must not fail silently."""
    server = env.get("GITHUB_SERVER_URL", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    run_url = (
        f"{server}/{repository}/actions/runs/{run_id}"
        if server and repository and run_id
        else ""
    )
    return DeployedMarker(
        environment=environment,
        manifest_digest=manifest_digest,
        policyengine_version=env.get("POLICYENGINE_VERSION", ""),
        us_version=env.get("POLICYENGINE_US_VERSION", ""),
        uk_version=env.get("POLICYENGINE_UK_VERSION", ""),
        us_data_version=env.get("US_DATA_VERSION", ""),
        uk_data_version=env.get("UK_DATA_VERSION", ""),
        github_run_id=run_id,
        github_run_url=run_url,
        github_sha=env.get("GITHUB_SHA", ""),
        deployed_at=datetime.now(timezone.utc).isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--manifest-digest", required=True)
    args = parser.parse_args()

    marker = build_marker(args.environment, args.manifest_digest, os.environ)
    # The store speaks JSON-able mappings; models dump at the call site
    # (same discipline as publish_manifest_impl).
    ArtifactStore().write_deployed_marker(args.environment, marker.model_dump())
    print(
        f"Recorded deployed/{args.environment}.json for manifest {args.manifest_digest}"
    )


if __name__ == "__main__":
    main()
