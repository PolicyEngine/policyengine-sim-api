"""Describe the release bundle a deployed simulation app serves."""

from typing import NotRequired, TypedDict


class CountryBundleMetadata(TypedDict):
    """One country's model package, data release and dataset URIs in a bundle."""

    country: str
    model_package_name: str
    model_version: str
    data_package_name: str
    data_package_version: str
    data_version: str
    data_artifact_revision: str
    default_dataset: str
    default_dataset_uri: str
    dataset_uris: dict[str, str]
    dataset_repo_types: dict[str, str]
    default_dataset_sha256: NotRequired[str | None]


class BundleManifestMetadata(TypedDict):
    """The release bundle served by one deployed app, keyed by its policyengine version.

    ``spm`` is present only when the runtime reports an SPM capability.
    """

    app_name: str
    policyengine_version: str
    us: CountryBundleMetadata
    uk: CountryBundleMetadata
    spm: NotRequired[dict]
    # Apps deployed before stage reporting have no key; the gateway then
    # reports no stage for their jobs.
    job_progress: NotRequired[int]
    # Present only for apps whose bundle routes UK area regions to a
    # local-area dataset; the gateway refuses breakdown=local_areas for any
    # other app, which would ignore the field and return a national result.
    local_area_breakdown: NotRequired[int]


def _country_bundle_metadata(country: str) -> CountryBundleMetadata:
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # the executor package loads only when bundle metadata is built
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle(country)
    return {
        "country": bundle.country,
        "model_package_name": bundle.model_package_name,
        "model_version": bundle.model_version,
        "data_package_name": bundle.data_package_name,
        "data_package_version": bundle.data_package_version,
        "data_version": bundle.data_version,
        "data_artifact_revision": bundle.data_artifact_revision,
        "default_dataset": bundle.default_dataset,
        "default_dataset_uri": bundle.default_dataset_uri,
        "dataset_uris": dict(bundle.dataset_uris),
        "dataset_repo_types": dict(bundle.dataset_repo_types),
        "default_dataset_sha256": bundle.default_dataset_sha256,
    }


def build_bundle_manifest_metadata(
    *,
    app_name: str,
    policyengine_version: str,
) -> BundleManifestMetadata:
    """Describe the installed release bundle as served by ``app_name``."""
    from policyengine_simulation_contract.job_progress import (  # noqa: PLC0415  # the contract package loads only when bundle metadata is built
        JOB_PROGRESS_CAPABILITY_VERSION,
    )
    from policyengine_simulation_contract.local_area_breakdown import (  # noqa: PLC0415  # the contract package loads only when bundle metadata is built
        LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION,
    )
    from policyengine_simulation_executor.spm import (
        runtime_spm_capability,
    )
    from policyengine_simulation_executor.uk_local_authority_metadata import (  # noqa: PLC0415  # the executor package loads only when bundle metadata is built
        uk_area_regions_routed,
    )

    capability = runtime_spm_capability()
    metadata: BundleManifestMetadata = {
        "app_name": app_name,
        "policyengine_version": policyengine_version,
        "us": _country_bundle_metadata("us"),
        "uk": _country_bundle_metadata("uk"),
        "job_progress": JOB_PROGRESS_CAPABILITY_VERSION,
    }
    if capability is not None:
        metadata["spm"] = capability.model_dump(mode="json")
    if uk_area_regions_routed():
        metadata["local_area_breakdown"] = LOCAL_AREA_BREAKDOWN_CAPABILITY_VERSION
    return metadata
