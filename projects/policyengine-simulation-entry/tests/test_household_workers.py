"""Finding the default v2 worker's household function for a country."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from household_fixtures import (
    BUNDLE_MANIFEST_SHA256,
    POLICYENGINE_VERSION,
    household_worker_version,
    manifest_store,
)
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_entry import household_workers
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    ManifestHouseholdWorkers,
    ModalHouseholdFunctionCaller,
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


class FakeModalFunction:
    """A deployed Modal function, as ``modal.Function.from_name`` returns it."""

    def __init__(self, call_id: str | None) -> None:
        self.call_id = call_id
        self.spawned: list[tuple[tuple, dict]] = []
        self.spawn = SimpleNamespace(aio=self._spawn)

    async def _spawn(self, *arguments, **keywords):
        self.spawned.append((arguments, keywords))
        return SimpleNamespace(object_id=self.call_id)


JOB_ID = UUID("00000000-0000-4000-8000-0000000000aa")


@pytest.mark.asyncio
async def test_a_household_function_is_called_by_name_with_its_job(monkeypatch):
    function = FakeModalFunction("fc-household-1")
    looked_up: list[tuple] = []

    def from_name(application_name, function_name, *, environment_name):
        looked_up.append((application_name, function_name, environment_name))
        return function

    monkeypatch.setattr(household_workers.modal.Function, "from_name", from_name)
    worker = ManifestHouseholdWorkers(
        manifest_store(household_worker_version())
    ).default_worker("us")

    call_id = await ModalHouseholdFunctionCaller("staging").spawn(
        worker,
        {"calculation_id": str(JOB_ID)},
        household_job_id=JOB_ID,
        observability_context={"observability_id": "o-1"},
    )

    assert call_id == "fc-household-1"
    assert looked_up == [
        (v2_application_name(POLICYENGINE_VERSION), "calculate_household_us", "staging")
    ]
    assert function.spawned == [
        (
            ({"calculation_id": str(JOB_ID)},),
            {
                "household_job_id": str(JOB_ID),
                "observability_context": {"observability_id": "o-1"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_call_modal_gives_no_id_is_refused(monkeypatch):
    monkeypatch.setattr(
        household_workers.modal.Function,
        "from_name",
        lambda *names, environment_name: FakeModalFunction(None),
    )
    worker = ManifestHouseholdWorkers(
        manifest_store(household_worker_version())
    ).default_worker("uk")

    with pytest.raises(RuntimeError, match="no call ID"):
        await ModalHouseholdFunctionCaller("staging").spawn(
            worker, {}, household_job_id=JOB_ID, observability_context=None
        )


def test_the_household_function_for_a_jobs_version_is_found_when_not_the_default() -> (
    None
):
    """A job keeps the .py version it was created on after the default moves."""
    workers = ManifestHouseholdWorkers(
        manifest_store(household_worker_version(), household_worker_version("6.18.0"))
    )

    worker = workers.worker_for_version("us", "6.18.0")

    assert worker.bundle.policyengine_version == "6.18.0"
    assert worker.application_name == v2_application_name("6.18.0")
    assert worker.function_name == "calculate_household_us"


def test_a_version_the_manifest_no_longer_holds_has_no_household_function() -> None:
    workers = ManifestHouseholdWorkers(manifest_store(household_worker_version()))

    with pytest.raises(HouseholdFunctionUnavailableError) as raised:
        workers.worker_for_version("us", "6.0.0")

    assert str(raised.value) == "the v2 version manifest has no worker for .py 6.0.0"


def test_a_household_version_lookup_reads_an_unreadable_store_as_unavailable() -> None:
    with pytest.raises(VersionManifestUnavailableError):
        ManifestHouseholdWorkers({}).worker_for_version("us", POLICYENGINE_VERSION)
