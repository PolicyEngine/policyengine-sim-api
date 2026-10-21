# pyright: reportAttributeAccessIssue=false, reportFunctionMemberAccess=false
"""Pre-merge smoke for static files inside the exact UK Stage 12 image."""

from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
)
from policyengine_simulation_executor.release_bundle import (
    resolve_local_bundle_dataset_path,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata_from_hdf,
    load_uk_local_authority_resources,
    uk_area_hdf_dataset_path,
)
from src.modal.v2_app import hf_secret, uk_worker_image

import modal

app = modal.App("policyengine-stage12-v2-static-runtime-files-smoke")


def validate_installed_uk_local_authority_dataset() -> str:
    """Return the local authority boundary version of the UK area results' dataset.

    That is the installed UK dataset, unless it carries no area codes and the
    bundle routes area regions to another dataset; then the routed dataset is
    downloaded and checked instead (``uk_area_hdf_dataset_path``). Raises if
    the image has no installed certified UK dataset.
    """
    dataset_path = resolve_local_bundle_dataset_path("uk", None)
    if dataset_path is None:
        msg = "installed certified UK dataset is unavailable"
        raise RuntimeError(msg)
    return detect_uk_local_authority_metadata_from_hdf(
        uk_area_hdf_dataset_path(dataset_path)
    ).boundary_version.value


# The memory covers reading a routed dataset's authority codes.
@app.function(image=uk_worker_image, timeout=600, memory=2048, secrets=[hf_secret])
def smoke_stage12_static_runtime_files() -> dict[str, int | str]:
    """Count the UK local authority files inside the UK worker image."""
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
    """Run the smoke remotely and raise unless counts and boundary version match."""
    observed = smoke_stage12_static_runtime_files.remote()
    expected_counts = {
        "names": 378,
        "lad22_coordinates": 374,
        "lad23_coordinates": 361,
    }
    if {key: observed.get(key) for key in expected_counts} != expected_counts:
        msg = (
            f"Stage 12 static runtime file smoke returned invalid counts: {observed!r}"
        )
        raise RuntimeError(msg)
    supported_versions = {version.value for version in UKLocalAuthorityBoundaryVersion}
    if observed.get("installed_dataset_boundary_version") not in supported_versions:
        msg = (
            "Stage 12 static runtime file smoke could not identify the installed "
            f"UK dataset boundary version: {observed!r}"
        )
        raise RuntimeError(msg)
    print("Stage 12 static runtime file smoke OK")  # noqa: T201  # Modal local entrypoint: runs locally, and stdout is the smoke's report
