"""Test values for the household calculation routes.

The household path keeps its own fixtures, apart from the Stage 12
comparison path's, so neither depends on the other's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from policyengine_simulation_contract.stage12_bundle import (
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_manifest import (
    V2CountryWorker,
    V2WorkerValidation,
    V2WorkerVersion,
    add_v2_worker,
    read_v2_manifest,
    v2_application_name,
    write_v2_manifest,
)

POLICYENGINE_VERSION = "6.17.1"
CORE_PACKAGE_VERSION = "3.33.3"
COUNTRY_PACKAGE_VERSIONS = {"us": "2.2.1", "uk": "2.174.1"}
BUNDLE_MANIFEST_SHA256 = "b" * 64


def _country_bundle(country: str) -> Stage12CountryBundle:
    package = f"policyengine-{country}"
    package_version = COUNTRY_PACKAGE_VERSIONS[country]
    dataset = f"populace_{country}_2024"
    revision = f"{dataset}-revision"
    return Stage12CountryBundle(
        country=country,
        country_package_name=package,
        country_package_version=package_version,
        country_package_requirement=f"{package}=={package_version}",
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_release_version=revision,
        data_artifact_revision=revision,
        default_dataset=dataset,
        default_dataset_uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
        datasets=(
            Stage12Dataset(
                identity=dataset,
                uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
                artifact_revision=revision,
                sha256="a" * 64,
                repo_type="dataset",
            ),
        ),
    )


def household_worker_version(
    policyengine_version: str = POLICYENGINE_VERSION,
    *,
    household_countries: tuple[str, ...] = ("us", "uk"),
) -> V2WorkerVersion:
    """Return a validated v2 worker for the US and the UK.

    Each country runs economy reports; those in ``household_countries`` also
    have a household function.
    """
    country_workers = tuple(
        V2CountryWorker(
            country=country,
            economy_capable=True,
            single_simulation_callable=f"run_single_simulation_{country}",
            household_callable=(
                f"calculate_household_{country}"
                if country in household_countries
                else None
            ),
        )
        for country in ("us", "uk")
    )
    application_name = v2_application_name(policyengine_version)
    return V2WorkerVersion(
        application_name=application_name,
        report_coordinator_callable="coordinate_report",
        countries=country_workers,
        bundle=Stage12BundleManifest(
            policyengine_version=policyengine_version,
            policyengine_requirement=f"policyengine=={policyengine_version}",
            core_package_version=CORE_PACKAGE_VERSION,
            core_package_requirement=f"policyengine-core=={CORE_PACKAGE_VERSION}",
            countries=(_country_bundle("us"), _country_bundle("uk")),
        ),
        bundle_manifest_sha256=BUNDLE_MANIFEST_SHA256,
        validation=V2WorkerValidation(
            validated=True,
            application_name=application_name,
            bundle_manifest_sha256=BUNDLE_MANIFEST_SHA256,
            validated_at=datetime(2026, 11, 10, tzinfo=UTC),
            validation_invocation_id="validation-1",
            country_validation_invocation_ids={
                country: f"validation-{country}" for country in ("us", "uk")
            },
            household_validation_invocation_ids={
                country: f"household-validation-{country}"
                for country in household_countries
            },
        ),
    )


def manifest_store(
    default_worker: V2WorkerVersion, *other_workers: V2WorkerVersion
) -> dict[str, object]:
    """Return a manifest store whose default is ``default_worker``.

    The other workers are added after it without moving the default, as a
    deployment adds its worker before its checks.
    """
    store: dict[str, object] = {}
    for worker in (default_worker, *other_workers):
        write_v2_manifest(
            store=store,
            manifest=add_v2_worker(current=read_v2_manifest(store), worker=worker),
        )
    return store
