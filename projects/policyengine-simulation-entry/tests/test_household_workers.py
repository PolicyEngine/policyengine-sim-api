"""Finding a country's household function in the v2 version manifest."""

from __future__ import annotations

import pytest
from household_fakes import POLICYENGINE_VERSION, manifest_workers
from policyengine_simulation_contract.stage12_manifest import (
    publish_v2_manifest,
    v2_application_name,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    HouseholdWorkersUnavailableError,
    ManifestHouseholdWorkers,
)
from policyengine_simulation_entry.modal_calls import ModalTarget
from stage12_fixtures import worker


def test_a_countrys_household_function_and_bundle_come_from_the_manifest() -> None:
    household_worker = manifest_workers().resolve("uk", POLICYENGINE_VERSION)

    assert household_worker.target == ModalTarget(
        application=v2_application_name(POLICYENGINE_VERSION),
        function="calculate_household_uk",
    )
    assert household_worker.bundle.policyengine_version == POLICYENGINE_VERSION
    assert household_worker.bundle.country_package_name == "policyengine-uk"
    assert household_worker.bundle.country_package_version == "2.90.2"
    assert household_worker.bundle.core_package_version == "3.30.1"
    assert household_worker.bundle.bundle_manifest_sha256 == "b" * 64


def test_a_version_the_manifest_lacks_has_no_household_function() -> None:
    with pytest.raises(HouseholdFunctionUnavailableError, match=r"6\.0\.0"):
        manifest_workers().resolve("us", "6.0.0")


def test_a_country_without_a_household_function_has_none() -> None:
    validated = worker()
    economy_only = validated.model_copy(
        update={
            "countries": tuple(
                country.model_copy(update={"household_callable": None})
                if country.country == "us"
                else country
                for country in validated.countries
            ),
            "validation": validated.validation.model_copy(
                update={"household_validation_invocation_ids": {"uk": "household-uk"}}
            ),
        }
    )
    store: dict[str, object] = {}
    publish_v2_manifest(store=store, worker=economy_only)

    with pytest.raises(HouseholdFunctionUnavailableError, match="us"):
        ManifestHouseholdWorkers.from_store(store).resolve("us", POLICYENGINE_VERSION)


def test_an_empty_manifest_store_means_the_workers_are_unavailable() -> None:
    with pytest.raises(HouseholdWorkersUnavailableError):
        ManifestHouseholdWorkers.from_store({}).resolve("us", POLICYENGINE_VERSION)


class UnreachableStore:
    """A manifest store whose every read fails, as an unreachable Modal Dict does."""

    def get(self, key: str, default: object = None) -> object:
        """Fail the read."""
        msg = "Modal is unreachable"
        raise ConnectionError(msg)

    def __setitem__(self, key: str, value: object) -> None:
        """Fail the write."""
        raise ConnectionError(key)


def test_an_unreachable_manifest_store_means_the_workers_are_unavailable() -> None:
    with pytest.raises(HouseholdWorkersUnavailableError):
        ManifestHouseholdWorkers.from_store(UnreachableStore()).resolve(
            "us", POLICYENGINE_VERSION
        )
