# pyright: reportAttributeAccessIssue=false, reportFunctionMemberAccess=false
"""Pre-merge smoke for static files inside the exact UK Stage 12 image."""

import modal
from src.modal.v2_app import uk_worker_image

app = modal.App("policyengine-stage12-v2-static-runtime-files-smoke")


@app.function(image=uk_worker_image, timeout=600, memory=512)
def smoke_stage12_static_runtime_files() -> dict[str, int]:
    from policyengine_simulation_contract.stage12_execution import (
        UKLocalAuthorityBoundaryVersion,
    )

    from policyengine_simulation_executor.stage12_runtime.uk_local_authority_metadata import (
        load_uk_local_authority_resources,
    )

    resources = load_uk_local_authority_resources()
    return {
        "names": len(resources.names),
        "lad22_coordinates": len(
            resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD22]
        ),
        "lad23_coordinates": len(
            resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD23]
        ),
    }


@app.local_entrypoint()
def main() -> None:
    observed = smoke_stage12_static_runtime_files.remote()
    expected = {
        "names": 378,
        "lad22_coordinates": 374,
        "lad23_coordinates": 361,
    }
    if observed != expected:
        raise RuntimeError(
            f"Stage 12 static runtime file smoke returned {observed!r}, "
            f"expected {expected!r}"
        )
    print("Stage 12 static runtime file smoke OK")
