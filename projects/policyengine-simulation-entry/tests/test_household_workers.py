"""Finding the default v2 worker's household function for a country."""

from __future__ import annotations

import pytest
from household_fixtures import (
    BUNDLE_MANIFEST_SHA256,
    POLICYENGINE_VERSION,
    household_worker_version,
    manifest_store,
)
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    ManifestHouseholdWorkers,
    VersionManifestUnavailableError,
)


def test_the_default_workers_household_function_and_bundle_are_used() -> None:
    workers = ManifestHouseholdWorkers(manifest_store(household_worker_version()))

    worker = workers.default_worker("uk")

    assert worker.application_name == v2_application_name(POLICYENGINE_VERSION)
    assert worker.function_name == "calculate_household_uk"
    assert worker.bundle.policyengine_version == POLICYENGINE_VERSION
    assert worker.bundle.core_package_version == "3.33.3"
    assert worker.bundle.country_package_name == "policyengine-uk"
    assert worker.bundle.country_package_version == "2.174.1"
    assert worker.bundle.bundle_manifest_sha256 == BUNDLE_MANIFEST_SHA256


def test_a_newer_worker_is_not_used_until_it_is_the_default() -> None:
    """A deployment adds its worker before its checks and moves the default after."""
    workers = ManifestHouseholdWorkers(
        manifest_store(
            household_worker_version("6.17.1"), household_worker_version("6.18.0")
        )
    )

    assert workers.default_worker("us").bundle.policyengine_version == "6.17.1"


def test_a_default_worker_without_the_countrys_household_function_says_so() -> None:
    workers = ManifestHouseholdWorkers(
        manifest_store(household_worker_version(household_countries=("uk",)))
    )

    with pytest.raises(HouseholdFunctionUnavailableError) as raised:
        workers.default_worker("us")

    assert str(raised.value) == (
        f"the default v2 worker, .py {POLICYENGINE_VERSION}, has no household "
        "function for us"
    )


def test_an_empty_manifest_store_is_unavailable() -> None:
    with pytest.raises(VersionManifestUnavailableError):
        ManifestHouseholdWorkers({}).default_worker("us")


class UnreachableStore:
    """A manifest store whose every read fails, as an unreachable Modal Dict does."""

    def get(self, key: str, default: object = None) -> object:
        """Fail the read."""
        msg = f"Modal is unreachable for {key}"
        raise ConnectionError(msg)

    def __setitem__(self, key: str, value: object) -> None:
        """Fail the write."""
        raise ConnectionError(key)


def test_an_unreachable_manifest_store_is_unavailable() -> None:
    with pytest.raises(VersionManifestUnavailableError):
        ManifestHouseholdWorkers(UnreachableStore()).default_worker("us")


class FlakyStore:
    """A manifest store that answers once, then fails every read."""

    def __init__(self, stored: dict[str, object]) -> None:
        self.stored = stored
        self.reads_allowed = True

    def get(self, key: str, default: object = None) -> object:
        """Answer while reads are allowed."""
        if not self.reads_allowed:
            msg = "Modal is unreachable"
            raise ConnectionError(msg)
        return self.stored.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        """Never written."""
        raise ConnectionError(key)


def test_a_store_that_stops_answering_is_unavailable_even_after_a_read() -> None:
    """Only an invalid stored manifest falls back to the last valid one."""
    store = FlakyStore(manifest_store(household_worker_version()))
    workers = ManifestHouseholdWorkers(store)
    workers.default_worker("us")
    store.reads_allowed = False

    with pytest.raises(VersionManifestUnavailableError):
        workers.default_worker("us")
