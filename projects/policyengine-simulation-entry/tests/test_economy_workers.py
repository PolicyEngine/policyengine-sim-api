"""The default v2 worker's economy functions, and starting their calls on Modal."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import modal
import pytest
from economy_fixtures import economy_manifest, economy_worker_version
from household_fixtures import (
    BUNDLE_MANIFEST_SHA256,
    POLICYENGINE_VERSION,
    household_worker_version,
    manifest_store,
)
from policyengine_simulation_contract.stage12_manifest import (
    V2CountryWorker,
    manifest_digest,
    v2_application_name,
)
from policyengine_simulation_entry import economy_workers
from policyengine_simulation_entry.economy_workers import (
    EconomyWorkerUnavailableError,
    ManifestEconomyWorkers,
    ModalEconomyRunCaller,
    default_economy_worker,
)
from policyengine_simulation_entry.household_workers import (
    VersionManifestUnavailableError,
)
from policyengine_simulation_persistence import RunVersions


def test_the_default_workers_economy_functions_and_bundle_are_found() -> None:
    manifest = economy_manifest()

    worker = default_economy_worker(manifest, "us")

    assert worker.version == POLICYENGINE_VERSION
    assert worker.version_manifest_sha256 == manifest_digest(manifest)
    assert worker.application_name == v2_application_name(POLICYENGINE_VERSION)
    assert worker.coordinator_callable == "coordinate_report"
    assert worker.simulation_callable == "run_single_simulation_us"
    assert worker.bundle_manifest_sha256 == BUNDLE_MANIFEST_SHA256
    assert worker.country.country == "us"
    assert worker.versions == RunVersions(
        country_package_version="2.2.1", policyengine_version=POLICYENGINE_VERSION
    )


def test_a_country_the_default_worker_cannot_calculate_is_refused() -> None:
    base = economy_worker_version()
    household_only = base.model_copy(
        update={
            "countries": tuple(
                V2CountryWorker(
                    country=item.country,
                    economy_capable=False,
                    single_simulation_callable=None,
                    household_callable=item.household_callable,
                )
                if item.country == "uk"
                else item
                for item in base.countries
            ),
            "validation": base.validation.model_copy(
                update={"country_validation_invocation_ids": {"us": "validation-us"}}
            ),
        }
    )
    manifest = economy_manifest(household_only)

    with pytest.raises(EconomyWorkerUnavailableError, match="uk"):
        default_economy_worker(manifest, "uk")
    assert default_economy_worker(manifest, "us").country.country == "us"


def test_the_manifest_store_is_read_for_the_default_worker() -> None:
    newer = household_worker_version("6.18.0")
    workers = ManifestEconomyWorkers(manifest_store(household_worker_version(), newer))

    worker = workers.default_worker("uk")

    assert worker.version == POLICYENGINE_VERSION
    assert worker.simulation_callable == "run_single_simulation_uk"


class UnreadableStore:
    def get(self, key: str, default: object = None) -> object:
        msg = "Modal is unreachable"
        raise ConnectionError(msg)

    def __setitem__(self, key: str, value: object) -> None:
        raise AssertionError


def test_a_manifest_that_cannot_be_read_is_unavailable() -> None:
    with pytest.raises(VersionManifestUnavailableError) as raised:
        ManifestEconomyWorkers(UnreadableStore()).default_worker("us")

    assert isinstance(raised.value.__cause__, ConnectionError)


class FakeFunction:
    """A Modal function looked up by name, recording each spawn."""

    def __init__(self, object_id: object) -> None:
        self.object_id = object_id
        self.spawns: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.spawn = SimpleNamespace(aio=self._spawn)

    async def _spawn(self, *arguments: Any, **keywords: Any) -> SimpleNamespace:
        self.spawns.append((arguments, keywords))
        return SimpleNamespace(object_id=self.object_id)


@pytest.fixture
def modal_functions(monkeypatch):
    """Replace Modal's lookup by name; return the lookups and the function."""
    looked_up: list[tuple[str, str, str]] = []
    function = FakeFunction("fc-started")

    def from_name(application: str, name: str, *, environment_name: str):
        looked_up.append((application, name, environment_name))
        return function

    monkeypatch.setattr(
        economy_workers.modal.Function, "from_name", staticmethod(from_name)
    )
    return looked_up, function


# Each worker class defines only its own country's method.
WORKER_METHOD_BY_CLASS = {
    "SimulationWorkerUS": "run_single_simulation_us",
    "SimulationWorkerUK": "run_single_simulation_uk",
}


class FakeWorkerClass:
    """A Modal class looked up by name, whose worker method spawns ``method``.

    The instance has only the looked-up class's method, under its name
    (``method_name``). With ``deployed`` False, hydrating it fails as it does
    for an app deployed before the workers were classes.
    """

    def __init__(self, method: FakeFunction) -> None:
        self.method = method
        self.method_name: str | None = None
        self.deployed = True
        self.hydrate = SimpleNamespace(aio=self._hydrate)

    async def _hydrate(self) -> FakeWorkerClass:
        if not self.deployed:
            msg = "the app defines no such class"
            raise modal.exception.NotFoundError(msg)
        return self

    def __call__(self) -> SimpleNamespace:
        assert self.method_name is not None
        return SimpleNamespace(**{self.method_name: self.method})


@pytest.fixture
def modal_worker_classes(monkeypatch):
    """Replace Modal's class lookup by name; return the lookups and the class."""
    looked_up: list[tuple[str, str, str]] = []
    worker_class = FakeWorkerClass(FakeFunction("fc-started-on-the-class"))

    def from_name(application: str, name: str, *, environment_name: str):
        looked_up.append((application, name, environment_name))
        worker_class.method_name = WORKER_METHOD_BY_CLASS[name]
        return worker_class

    monkeypatch.setattr(economy_workers.modal.Cls, "from_name", staticmethod(from_name))
    return looked_up, worker_class


def test_a_report_run_starts_the_coordinator_with_its_report_and_context(
    modal_functions,
) -> None:
    looked_up, function = modal_functions
    worker = default_economy_worker(economy_manifest(), "us")

    call_id = asyncio.run(
        ModalEconomyRunCaller("staging").start_report_run(
            worker,
            {"report": "payload"},
            {"report_run": "context"},
            observability_context={"observability_id": "observed"},
        )
    )

    assert call_id == "fc-started"
    assert looked_up == [(worker.application_name, "coordinate_report", "staging")]
    # No evaluation record follows the context.
    assert function.spawns == [
        (
            ({"report": "payload"}, {"report_run": "context"}),
            {"observability_context": {"observability_id": "observed"}},
        )
    ]


def test_a_standalone_run_starts_the_countrys_simulation_worker_class(
    modal_functions, modal_worker_classes
) -> None:
    looked_up_functions, _function = modal_functions
    looked_up_classes, worker_class = modal_worker_classes
    worker = default_economy_worker(economy_manifest(), "uk")

    call_id = asyncio.run(
        ModalEconomyRunCaller("main").start_simulation_run(
            worker,
            {"simulation": 1},
            {"simulation_run_id": "run"},
            observability_context=None,
        )
    )

    # The class's containers restore from a memory snapshot with the models
    # loaded; the manifest names the worker by its method.
    assert call_id == "fc-started-on-the-class"
    assert looked_up_classes == [
        (worker.application_name, "SimulationWorkerUK", "main")
    ]
    assert looked_up_functions == []
    assert worker_class.method.spawns == [
        (
            ({"simulation": 1}, {"simulation_run_id": "run"}),
            {"observability_context": None},
        )
    ]


def test_a_cliff_calculation_starts_the_countrys_simulation_worker_class(
    modal_functions, modal_worker_classes
) -> None:
    """C1 · 38's cliff mode: the context names the cliff result, not a run."""
    looked_up_functions, _function = modal_functions
    looked_up_classes, worker_class = modal_worker_classes
    worker = default_economy_worker(economy_manifest(), "uk")

    call_id = asyncio.run(
        ModalEconomyRunCaller("main").start_cliff_calculation(
            worker,
            {"simulation": 1},
            {"cliff_result_id": "cliffs"},
            observability_context={"observability_id": "observed"},
        )
    )

    assert call_id == "fc-started-on-the-class"
    assert looked_up_classes == [
        (worker.application_name, "SimulationWorkerUK", "main")
    ]
    assert looked_up_functions == []
    assert worker_class.method.spawns == [
        (
            ({"simulation": 1}, {"cliff_result_id": "cliffs"}),
            {"observability_context": {"observability_id": "observed"}},
        )
    ]


@pytest.mark.parametrize("start", ["start_simulation_run", "start_cliff_calculation"])
def test_an_app_without_the_worker_class_gets_the_simulation_function(
    modal_functions, modal_worker_classes, start
) -> None:
    # An app deployed for an older .py version, which the manifest's default
    # can name after a move or a rollback, defines the worker as a function.
    looked_up_functions, function = modal_functions
    _looked_up_classes, worker_class = modal_worker_classes
    worker_class.deployed = False
    worker = default_economy_worker(economy_manifest(), "us")

    call_id = asyncio.run(
        getattr(ModalEconomyRunCaller("main"), start)(
            worker, {"simulation": 1}, {"run": "context"}, observability_context=None
        )
    )

    assert call_id == "fc-started"
    assert looked_up_functions == [
        (worker.application_name, "run_single_simulation_us", "main")
    ]
    assert worker_class.method.spawns == []
    assert function.spawns == [
        (({"simulation": 1}, {"run": "context"}), {"observability_context": None})
    ]


@pytest.mark.parametrize("object_id", [None, ""])
def test_a_class_call_modal_gives_no_id_is_an_error(
    modal_worker_classes, object_id
) -> None:
    _looked_up, worker_class = modal_worker_classes
    worker_class.method.object_id = object_id
    worker = default_economy_worker(economy_manifest(), "us")

    with pytest.raises(RuntimeError, match="no call ID"):
        asyncio.run(
            ModalEconomyRunCaller("staging").start_simulation_run(
                worker, {}, {}, observability_context=None
            )
        )


@pytest.mark.parametrize("object_id", [None, ""])
def test_a_call_modal_gives_no_id_is_an_error(modal_functions, object_id) -> None:
    _, function = modal_functions
    function.object_id = object_id
    worker = default_economy_worker(economy_manifest(), "us")

    with pytest.raises(RuntimeError, match="no call ID"):
        asyncio.run(
            ModalEconomyRunCaller("staging").start_report_run(
                worker, {}, {}, observability_context=None
            )
        )
