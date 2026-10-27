"""v2 version manifests for the tests of the manifest's ``/versions`` answers.

Shared by the tests of the answers themselves and of the internal routes that
serve them.
"""

from __future__ import annotations

from policyengine_simulation_contract.stage12_manifest import (
    V2VersionManifest,
    V2WorkerVersion,
    v2_application_name,
)
from stage12_fixtures import worker


def manifest_worker(
    policyengine_version: str,
    *,
    us_version: str,
    uk_version: str,
) -> V2WorkerVersion:
    """Return the fixture worker, rebuilt for these package versions."""
    worker_document = worker().model_dump(mode="json")
    application_name = v2_application_name(policyengine_version)
    worker_document["application_name"] = application_name
    worker_document["validation"]["application_name"] = application_name
    worker_document["bundle"]["policyengine_version"] = policyengine_version
    worker_document["bundle"]["policyengine_requirement"] = (
        f"policyengine=={policyengine_version}"
    )
    for country_bundle in worker_document["bundle"]["countries"]:
        package_version = {"us": us_version, "uk": uk_version}[
            country_bundle["country"]
        ]
        country_bundle["country_package_version"] = package_version
        country_bundle["country_package_requirement"] = (
            f"{country_bundle['country_package_name']}=={package_version}"
        )
    return V2WorkerVersion.model_validate(worker_document)


def version_manifest(
    *workers: V2WorkerVersion,
    default_version: str,
) -> V2VersionManifest:
    """Return a manifest holding ``workers``, keyed by their bundle versions."""
    return V2VersionManifest(
        generation="test-generation",
        default_version=default_version,
        versions={
            worker_version.bundle.policyengine_version: worker_version
            for worker_version in workers
        },
    )


def with_country_bundle(
    worker_version: V2WorkerVersion,
    country: str,
    **fields: object,
) -> V2WorkerVersion:
    """Return ``worker_version`` with ``fields`` replaced in one country's bundle."""
    worker_document = worker_version.model_dump(mode="json")
    for country_bundle in worker_document["bundle"]["countries"]:
        if country_bundle["country"] == country:
            country_bundle.update(fields)
    return V2WorkerVersion.model_validate(worker_document)


TWO_RELEASES = version_manifest(
    manifest_worker("6.2.6", us_version="1.700.0", uk_version="2.125.1"),
    manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.126.8"),
    default_version="6.2.7",
)
