# pyright: reportAttributeAccessIssue=false, reportFunctionMemberAccess=false
"""Pre-merge smoke for static files inside the exact UK Stage 12 image."""

import modal
from src.modal.v2_app import uk_worker_image

from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
)
from policyengine_simulation_executor.release_bundle import (
    resolve_local_bundle_dataset_path,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata_from_hdf,
    load_uk_local_authority_resources,
)

app = modal.App("policyengine-stage12-v2-static-runtime-files-smoke")


def validate_installed_uk_local_authority_dataset() -> str:
    dataset_path = resolve_local_bundle_dataset_path("uk", None)
    if dataset_path is None:
        raise RuntimeError("installed certified UK dataset is unavailable")
    return detect_uk_local_authority_metadata_from_hdf(
        dataset_path
    ).boundary_version.value


@app.function(image=uk_worker_image, timeout=600, memory=512)
def smoke_stage12_static_runtime_files() -> dict[str, int | str]:

    resources = load_uk_local_authority_resources()
    return {
        "names": len(resources.names),
        "lad22_coordinates": len(
            resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD22]
        ),
        "lad23_coordinates": len(
            resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD23]
        ),
        "installed_dataset_boundary_version": (
            validate_installed_uk_local_authority_dataset()
        ),
    }


@app.local_entrypoint()
def main() -> None:
    observed = smoke_stage12_static_runtime_files.remote()
    expected_counts = {
        "names": 378,
        "lad22_coordinates": 374,
        "lad23_coordinates": 361,
    }
    if {key: observed.get(key) for key in expected_counts} != expected_counts:
        raise RuntimeError(
            f"Stage 12 static runtime file smoke returned invalid counts: {observed!r}"
        )
    supported_versions = {version.value for version in UKLocalAuthorityBoundaryVersion}
    if observed.get("installed_dataset_boundary_version") not in supported_versions:
        raise RuntimeError(
            "Stage 12 static runtime file smoke could not identify the installed "
            f"UK dataset boundary version: {observed!r}"
        )
    print("Stage 12 static runtime file smoke OK")
