"""Test values for the economy report and simulation routes.

They build on the household path's v2 worker, which can calculate economy
runs for the US and the UK, with a second US dataset that state regions use.
"""

from __future__ import annotations

from household_fixtures import POLICYENGINE_VERSION, household_worker_version
from policyengine_simulation_contract.stage12_bundle import Stage12Dataset
from policyengine_simulation_contract.stage12_manifest import (
    V2VersionManifest,
    V2WorkerVersion,
)

STATE_DATASET = Stage12Dataset(
    identity="populace_us_2024_acs_local",
    uri="hf://policyengine/data/populace_us_2024_acs_local.h5@acs-local-revision",
    artifact_revision="acs-local-revision",
    sha256="c" * 64,
    repo_type="dataset",
)


def economy_worker_version(
    policyengine_version: str = POLICYENGINE_VERSION,
) -> V2WorkerVersion:
    """Return a v2 worker whose US bundle also holds the state dataset."""
    base = household_worker_version(policyengine_version)
    countries = tuple(
        country.model_copy(update={"datasets": (*country.datasets, STATE_DATASET)})
        if country.country == "us"
        else country
        for country in base.bundle.countries
    )
    return base.model_copy(
        update={"bundle": base.bundle.model_copy(update={"countries": countries})}
    )


def economy_manifest(
    default_worker: V2WorkerVersion | None = None,
) -> V2VersionManifest:
    """Return a manifest whose only worker, its default, is ``default_worker``."""
    worker = default_worker or economy_worker_version()
    version = worker.bundle.policyengine_version
    return V2VersionManifest(
        generation="test", default_version=version, versions={version: worker}
    )
