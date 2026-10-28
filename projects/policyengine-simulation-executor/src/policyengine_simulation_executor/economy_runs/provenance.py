"""The bundle provenance of the dataset a v2 economy run's catalog names."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
)

from policyengine_simulation_executor.stage12_runtime.catalog_population import (
    catalog_dataset,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        ResolvedStage12Bundle,
    )


def catalog_bundle_provenance(
    *,
    country: CountryId,
    policyengine_version: str,
    dataset_name: str,
    resolved: ResolvedStage12Bundle,
) -> tuple[BundleProvenance, DatasetPopulationInput]:
    """Return the provenance and population of the dataset the catalog names.

    Raise ValueError when this application installs another policyengine.py
    version than ``policyengine_version``, or a release without the dataset.
    """
    installed = resolved.bundle.policyengine_version
    if installed != policyengine_version:
        msg = (
            f"the call names PolicyEngine.py {policyengine_version}, but this "
            f"application installs PolicyEngine.py {installed}"
        )
        raise ValueError(msg)
    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == country
    )
    dataset = catalog_dataset(country_bundle, dataset_name)
    provenance = BundleProvenance(
        policyengine_version=installed,
        country_package_name=country_bundle.country_package_name,
        country_package_version=country_bundle.country_package_version,
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name=country_bundle.data_package_name,
            data_package_version=country_bundle.data_package_version,
        ),
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
    )
    population_input = DatasetPopulationInput(
        artifact=DatasetArtifactReference(
            uri=dataset.uri,
            media_type=DatasetArtifactMediaType.HDF5,
            content_sha256=dataset.sha256,
        )
    )
    return provenance, population_input
