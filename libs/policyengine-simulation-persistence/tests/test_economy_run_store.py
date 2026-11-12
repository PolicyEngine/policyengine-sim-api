"""The economy store: each read and write in a transaction of its own.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``). The store opens each transaction
through a function it is given; here each is a savepoint of one connection,
acting as the simulation service's role, which the test rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, get_args
from uuid import uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    OPTIONS,
    OUTPUT_BUCKET_PREFIX,
    POLICYENGINE_VERSION,
    VERSIONS,
    insert_economy_catalog,
    insert_reusable_run,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    CatalogRegion,
    EconomyCatalogRows,
    EconomyPopulation,
    EconomyRecordOutcomeUnknownError,
    EconomyRequest,
    EconomyRunStore,
    MissingPolicyParametersError,
    NewEconomyReport,
    NewEconomySimulation,
    OverlappingReformError,
    ReportRunStatus,
    ReportSideError,
    ReportSimulation,
    ReusableRun,
    RunReuse,
    SideRefusal,
    SimulationChoice,
    SimulationStatus,
    UnknownCatalogVersionError,
    UnknownDatasetError,
    UnknownDynamicError,
    UnknownPolicyError,
    UnknownRegionError,
    economy_run_store,
)
from public_api_rows import StoredValue, insert_dataset, insert_dynamic, insert_policy
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"
REQUEST = EconomyRequest(
    country="us",
    policyengine_version=POLICYENGINE_VERSION,
    region_code="us",
    dataset_name=None,
    year=2026,
)
REUSE = RunReuse(
    versions=VERSIONS, inputs=OPTIONS, output_uri_prefix=OUTPUT_BUCKET_PREFIX
)


def _store_on(connection: Connection) -> EconomyRunStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return EconomyRunStore(savepoint)


def test_a_reports_inputs_are_read_together(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(
            connection, parameter_names=(CTC_AMOUNT, ELASTICITY)
        )
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        reused = insert_reusable_run(connection, economy)
        act_as_the_service(connection)
        store = _store_on(connection)
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=dynamic_id)

        inputs = store.read_report_inputs(
            REQUEST, baseline=CURRENT_LAW, reform=reform_choice, reuse=REUSE
        )
        without_reuse = store.read_report_inputs(
            REQUEST, baseline=CURRENT_LAW, reform=reform_choice, reuse=None
        )

    catalog = inputs.catalog
    assert (
        catalog.catalog_version_id,
        catalog.model_id,
        catalog.region.id,
        catalog.dataset.id,
        catalog.dataset.name,
    ) == (
        economy.catalog.version_id,
        economy.catalog.model_id,
        economy.region_id,
        economy.dataset_id,
        "populace_us_2024",
    )
    assert catalog.population(2026) == economy.population()
    assert (inputs.baseline.choice, inputs.baseline.reform) == (CURRENT_LAW, {})
    assert inputs.reform.choice == reform_choice
    assert inputs.reform.reform == {
        CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000},
        ELASTICITY: {"2026-01-01.2200-12-31": -0.05},
    }
    assert inputs.baseline.reusable_run == ReusableRun(
        simulation_id=reused.simulation_id, simulation_run_id=reused.simulation_run_id
    )
    assert inputs.reform.reusable_run is None
    assert without_reuse.baseline.reusable_run is None


def test_the_two_sides_never_reuse_the_same_run(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        only = insert_reusable_run(connection, economy)
        act_as_the_service(connection)

        inputs = _store_on(connection).read_report_inputs(
            REQUEST, baseline=CURRENT_LAW, reform=CURRENT_LAW, reuse=REUSE
        )

    assert inputs.baseline.reusable_run == ReusableRun(
        simulation_id=only.simulation_id, simulation_run_id=only.simulation_run_id
    )
    assert inputs.reform.reusable_run is None


def test_a_named_dataset_is_read_instead_of_the_regions_default(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        local_id = insert_dataset(connection, economy.catalog, "acs_local_2024")
        act_as_the_service(connection)

        inputs = _store_on(connection).read_simulation_inputs(
            EconomyRequest(**{**vars(REQUEST), "dataset_name": "acs_local_2024"}),
            CURRENT_LAW,
        )

    assert (inputs.catalog.dataset.id, inputs.catalog.dataset.name) == (
        local_id,
        "acs_local_2024",
    )
    assert (inputs.simulation.choice, inputs.simulation.reform) == (CURRENT_LAW, {})
    assert inputs.simulation.reusable_run is None


@pytest.mark.parametrize(
    ("missing", "error_type"),
    [
        ("catalog version", UnknownCatalogVersionError),
        ("region", UnknownRegionError),
        ("dataset", UnknownDatasetError),
        ("policy", UnknownPolicyError),
        ("dynamic", UnknownDynamicError),
    ],
)
def test_each_missing_row_a_request_names_is_refused(
    public_api_database_url, missing: str, error_type: type[Exception]
) -> None:
    request_fields = {
        "catalog version": {"policyengine_version": "6.16.0"},
        "region": {"region_code": "state/zz"},
        "dataset": {"dataset_name": "no_such_dataset"},
    }.get(missing, {})
    choice = {
        "policy": SimulationChoice(policy_id=uuid4(), dynamic_id=None),
        "dynamic": SimulationChoice(policy_id=None, dynamic_id=uuid4()),
    }.get(missing, CURRENT_LAW)
    with rolled_back_transaction(public_api_database_url) as connection:
        insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        request = EconomyRequest(**{**vars(REQUEST), **request_fields})

        # A report names the side whose policy or dynamic is missing.
        side_refused = missing in {"policy", "dynamic"}
        with pytest.raises(ReportSideError if side_refused else error_type) as raised:
            store.read_report_inputs(
                request, baseline=CURRENT_LAW, reform=choice, reuse=REUSE
            )
        with pytest.raises(error_type):
            store.read_simulation_inputs(request, choice)

    if side_refused:
        assert raised.value.side == "reform"
        assert isinstance(raised.value.error, error_type)


def test_values_the_running_version_lacks_or_both_set_are_refused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(ELASTICITY,))
        saved_on = insert_economy_catalog(
            connection, country="uk", parameter_names=(CTC_AMOUNT,)
        )
        # A policy of the US saved on parameters the running version lacks.
        lacking_policy_id = insert_policy(
            connection,
            replace(saved_on.catalog, country="us"),
            [StoredValue(CTC_AMOUNT, 3_000, START)],
        )
        overlapping_policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.1, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        act_as_the_service(connection)
        store = _store_on(connection)

        with pytest.raises(MissingPolicyParametersError):
            store.read_simulation_inputs(
                REQUEST, SimulationChoice(lacking_policy_id, dynamic_id=None)
            )
        with pytest.raises(OverlappingReformError):
            store.read_simulation_inputs(
                REQUEST, SimulationChoice(overlapping_policy_id, dynamic_id)
            )


def _status(connection: Connection, table_name: str, run_id) -> tuple:
    return tuple(
        connection.execute(
            text(
                f"SELECT status, modal_call_id FROM public.{table_name} "  # noqa: S608  # fixed table names
                "WHERE id = :id"
            ),
            {"id": run_id},
        ).one()
    )


def test_a_report_is_created_then_its_call_recorded_or_its_run_failed(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        reports = [
            NewEconomyReport(
                id=uuid4(),
                run_id=uuid4(),
                country="us",
                label=f"Report {number}",
                description=None,
                tax_benefit_model_id=economy.catalog.model_id,
                population=economy.population(),
                versions=VERSIONS,
                inputs=OPTIONS,
                baseline=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
                reform=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
            )
            for number in (1, 2)
        ]
        for report in reports:
            store.create_report(report)
        recorded, refused = reports

        status = store.record_report_call(recorded.run_id, "fc-coordinator")
        failed = store.fail_unstarted_report(
            refused.run_id, refused.new_simulation_run_ids, "modal_call_refused: no"
        )
        recorded_state = _status(connection, "report_runs", recorded.run_id)
        refused_state = _status(connection, "report_runs", refused.run_id)
        refused_baseline = _status(
            connection, "simulation_runs", refused.baseline.simulation_run_id
        )

    assert (status, failed) == (ReportRunStatus.PENDING, True)
    assert recorded_state == ("pending", "fc-coordinator")
    assert refused_state == ("failed", None)
    assert refused_baseline == ("failed", None)


def test_a_simulation_is_created_then_its_call_recorded_or_its_run_failed(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        simulations = [
            NewEconomySimulation(
                id=uuid4(),
                run_id=uuid4(),
                choice=CURRENT_LAW,
                population=economy.population(),
                versions=VERSIONS,
            )
            for _ in range(2)
        ]
        for simulation in simulations:
            store.create_simulation(simulation)
        recorded, refused = simulations

        status = store.record_simulation_call(recorded.run_id, "fc-worker")
        failed = store.fail_unstarted_simulation(
            refused.run_id, "modal_call_refused: no"
        )
        recorded_state = _status(connection, "simulation_runs", recorded.run_id)
        refused_state = _status(connection, "simulation_runs", refused.run_id)

    assert (status, failed) == (SimulationStatus.PENDING, True)
    assert recorded_state == ("pending", "fc-worker")
    assert refused_state == ("failed", None)


def test_a_report_and_a_simulation_are_read_as_they_stand(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        store = _store_on(connection)
        report = NewEconomyReport(
            id=uuid4(),
            run_id=uuid4(),
            country="us",
            label="Read back",
            description=None,
            tax_benefit_model_id=economy.catalog.model_id,
            population=economy.population(),
            versions=VERSIONS,
            inputs=OPTIONS,
            baseline=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
            reform=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
        )
        store.create_report(report)
        store.record_report_call(report.run_id, "fc-coordinator")

        stored_report = store.read_report(report.id)
        stored_simulation = store.read_simulation(report.baseline.simulation_id)
        missing = (store.read_report(uuid4()), store.read_simulation(uuid4()))

    assert missing == (None, None)
    assert stored_report is not None
    assert stored_report.run is not None
    assert (stored_report.run.id, stored_report.run.status) == (
        report.run_id,
        ReportRunStatus.PENDING,
    )
    assert stored_simulation is not None
    assert stored_simulation.run is not None
    assert stored_simulation.run.id == report.baseline.simulation_run_id


@pytest.mark.parametrize(
    ("side", "error_type"),
    [
        ("baseline", UnknownDynamicError),
        ("reform", UnknownPolicyError),
        ("reform", MissingPolicyParametersError),
        ("baseline", OverlappingReformError),
    ],
)
def test_a_reports_refused_values_name_the_side_they_belong_to(
    public_api_database_url, side: str, error_type: type[Exception]
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(ELASTICITY,))
        saved_on = insert_economy_catalog(
            connection, country="uk", parameter_names=(CTC_AMOUNT,)
        )
        lacking_policy_id = insert_policy(
            connection,
            replace(saved_on.catalog, country="us"),
            [StoredValue(CTC_AMOUNT, 3_000, START)],
        )
        overlapping_policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.1, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        act_as_the_service(connection)
        refused = {
            UnknownDynamicError: SimulationChoice(None, uuid4()),
            UnknownPolicyError: SimulationChoice(uuid4(), None),
            MissingPolicyParametersError: SimulationChoice(lacking_policy_id, None),
            OverlappingReformError: SimulationChoice(overlapping_policy_id, dynamic_id),
        }[error_type]
        choices = {"baseline": CURRENT_LAW, "reform": CURRENT_LAW, side: refused}

        with pytest.raises(ReportSideError) as raised:
            _store_on(connection).read_report_inputs(
                REQUEST,
                baseline=choices["baseline"],
                reform=choices["reform"],
                reuse=None,
            )

    assert raised.value.side == side
    assert isinstance(raised.value.error, error_type)


class _CommitFails:
    """A transaction whose block runs, and whose commit then fails."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, error_type, error, traceback) -> None:
        if error is None:
            statement = "COMMIT"
            raise OperationalError(statement, {}, Exception("connection lost"))


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement, *arguments):
        self.statements.append(statement)


def _new_report() -> NewEconomyReport:
    return NewEconomyReport(
        id=uuid4(),
        run_id=uuid4(),
        country="us",
        label="Lost commit",
        description=None,
        tax_benefit_model_id=uuid4(),
        population=EconomyPopulation(
            tax_benefit_model_version_id=uuid4(),
            region_id=uuid4(),
            dataset_id=uuid4(),
            year=2026,
        ),
        versions=VERSIONS,
        inputs=OPTIONS,
        baseline=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
        reform=ReportSimulation(uuid4(), uuid4(), CURRENT_LAW, reused=False),
    )


def test_a_create_whose_commit_fails_may_have_been_written() -> None:
    connection = _RecordingConnection()
    store = EconomyRunStore(lambda: _CommitFails(connection))
    report = _new_report()

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        store.create_report(report)
    with pytest.raises(EconomyRecordOutcomeUnknownError):
        store.create_simulation(
            NewEconomySimulation(
                id=uuid4(),
                run_id=uuid4(),
                choice=CURRENT_LAW,
                population=report.population,
                versions=VERSIONS,
            )
        )

    assert isinstance(raised.value.__cause__, OperationalError)
    assert connection.statements


class _StatementFails:
    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback) -> None:
        return None

    def execute(self, statement, *arguments):
        failed_statement = "INSERT"
        raise OperationalError(failed_statement, {}, Exception("connection lost"))


def test_a_create_whose_statement_fails_is_not_written() -> None:
    store = EconomyRunStore(_StatementFails)

    with pytest.raises(OperationalError):
        store.create_report(_new_report())


def test_every_side_refusal_is_raised_with_its_side(monkeypatch) -> None:
    """Every error the side refusal type names is wrapped; none slips through."""
    catalog = EconomyCatalogRows(
        catalog_version_id=uuid4(),
        model_id=uuid4(),
        region=CatalogRegion(id=uuid4(), code="us", default_dataset_id=uuid4()),
        dataset=CatalogDataset(id=uuid4(), name="populace_us_2024"),
    )
    monkeypatch.setattr(economy_run_store, "_catalog_rows", lambda *_: catalog)
    refusals = {
        UnknownPolicyError: UnknownPolicyError("no policy"),
        UnknownDynamicError: UnknownDynamicError("no dynamic"),
        MissingPolicyParametersError: MissingPolicyParametersError(["gov.a"]),
        OverlappingReformError: OverlappingReformError(["gov.a"]),
    }
    assert set(get_args(SideRefusal.__value__)) == set(refusals)

    @contextmanager
    def no_database():
        yield None

    store = EconomyRunStore(no_database)
    for refusal in refusals.values():

        def refuse(*_, refusal=refusal):
            raise refusal

        monkeypatch.setattr(economy_run_store, "_simulation_inputs", refuse)
        with pytest.raises(ReportSideError) as raised:
            store.read_report_inputs(
                REQUEST, baseline=CURRENT_LAW, reform=CURRENT_LAW, reuse=None
            )
        assert (raised.value.side, raised.value.error) == ("baseline", refusal)
