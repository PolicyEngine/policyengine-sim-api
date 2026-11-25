"""The economy store: each read and write in a transaction of its own.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``). The store runs each operation in an ORM
session of its own; here each session's transaction is a savepoint of one
connection, acting as the simulation service's role, which the test rolls back.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, get_args
from uuid import uuid4

import pytest
from conftest import act_as_the_service, economy_store_on, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    POLICYENGINE_VERSION,
    VERSIONS,
    RunShape,
    insert_economy_catalog,
    insert_pending_report,
    insert_simulation_run,
    insert_uk_area_regions,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    CatalogRegion,
    DynamicWithoutValuesError,
    EconomyCatalogRows,
    EconomyPopulation,
    EconomyRecordOutcomeUnknownError,
    EconomyRequest,
    EconomyRunStore,
    LocalAreaDatasetMismatchError,
    MissingPolicyParametersError,
    NewReportRerun,
    OverlappingReformError,
    ReportRunStatus,
    ReportSideError,
    RerunSide,
    SideRefusal,
    SimulationChoice,
    SimulationStatus,
    UnknownCatalogVersionError,
    UnknownDatasetError,
    UnknownDynamicError,
    UnknownPolicyError,
    UnknownRegionError,
    economy_inputs,
    economy_run_store,
)
from policyengine_simulation_persistence.economy_inputs import read_catalog_rows
from public_api_rows import (
    StoredValue,
    insert_dataset,
    insert_dynamic,
    insert_parameter,
    insert_policy,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from transaction_fakes import CommitFailingTransaction, RecordingSession

if TYPE_CHECKING:
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
        act_as_the_service(connection)
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=dynamic_id)

        inputs = economy_store_on(connection).read_report_inputs(
            REQUEST, baseline=CURRENT_LAW, reform=reform_choice
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


UK_REQUEST = replace(REQUEST, country="uk", region_code="uk")
# The dataset the UK worker declares it calculates local areas on.
LOCAL_AREA_DATASET_NAME = "microcosm_uk_2024_25_dense"


def test_a_report_on_the_local_area_dataset_is_read_on_it(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        local_area_id = insert_dataset(
            connection, economy.catalog, LOCAL_AREA_DATASET_NAME
        )
        insert_uk_area_regions(
            connection,
            economy.catalog,
            constituency_dataset_id=local_area_id,
            local_authority_dataset_id=local_area_id,
        )
        act_as_the_service(connection)
        store = economy_store_on(connection)

        on_local_areas = store.read_report_inputs(
            UK_REQUEST,
            baseline=CURRENT_LAW,
            reform=CURRENT_LAW,
            local_area_dataset_name=LOCAL_AREA_DATASET_NAME,
        )
        national = store.read_report_inputs(
            UK_REQUEST, baseline=CURRENT_LAW, reform=CURRENT_LAW
        )

    # The local-area dataset takes the place of the region's default.
    assert on_local_areas.catalog.dataset == CatalogDataset(
        id=local_area_id, name=LOCAL_AREA_DATASET_NAME
    )
    assert national.catalog.dataset.id == economy.dataset_id
    assert on_local_areas.catalog == replace(
        national.catalog, dataset=on_local_areas.catalog.dataset
    )


@pytest.mark.parametrize(
    "area_datasets", ["another dataset", "a dataset for each type", "no area regions"]
)
def test_a_declared_dataset_the_catalog_does_not_give_both_area_types_is_refused(
    public_api_database_url, area_datasets
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        local_area_id = insert_dataset(
            connection, economy.catalog, LOCAL_AREA_DATASET_NAME
        )
        other_id = insert_dataset(connection, economy.catalog, "microcosm_uk_2024_25")
        if area_datasets != "no area regions":
            insert_uk_area_regions(
                connection,
                economy.catalog,
                constituency_dataset_id=(
                    other_id if area_datasets == "another dataset" else local_area_id
                ),
                local_authority_dataset_id=other_id,
            )
        act_as_the_service(connection)

        with pytest.raises(LocalAreaDatasetMismatchError) as mismatch:
            economy_store_on(connection).read_report_inputs(
                UK_REQUEST,
                baseline=CURRENT_LAW,
                reform=CURRENT_LAW,
                local_area_dataset_name=LOCAL_AREA_DATASET_NAME,
            )

    assert mismatch.value.declared_dataset == LOCAL_AREA_DATASET_NAME


def test_a_named_dataset_is_read_instead_of_the_regions_default(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        local_id = insert_dataset(connection, economy.catalog, "acs_local_2024")
        act_as_the_service(connection)

        inputs = economy_store_on(connection).read_simulation_inputs(
            EconomyRequest(**{**vars(REQUEST), "dataset_name": "acs_local_2024"}),
            CURRENT_LAW,
        )

    assert (inputs.catalog.dataset.id, inputs.catalog.dataset.name) == (
        local_id,
        "acs_local_2024",
    )
    assert (inputs.simulation.choice, inputs.simulation.reform) == (CURRENT_LAW, {})


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
        store = economy_store_on(connection)
        request = EconomyRequest(**{**vars(REQUEST), **request_fields})

        # A report names the side whose policy or dynamic is missing.
        side_refused = missing in {"policy", "dynamic"}
        with pytest.raises(ReportSideError if side_refused else error_type) as raised:
            store.read_report_inputs(request, baseline=CURRENT_LAW, reform=choice)
        with pytest.raises(error_type):
            store.read_simulation_inputs(request, choice)

    if side_refused:
        assert raised.value.side == "reform"
        assert isinstance(raised.value.error, error_type)


def test_a_dynamic_of_another_country_is_refused_on_every_economy_read(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        insert_economy_catalog(connection, parameter_names=(ELASTICITY,))
        uk_economy = insert_economy_catalog(
            connection, country="uk", parameter_names=(ELASTICITY,)
        )
        uk_dynamic_id = insert_dynamic(
            connection, uk_economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        act_as_the_service(connection)
        store = economy_store_on(connection)
        uk_dynamic = SimulationChoice(policy_id=None, dynamic_id=uk_dynamic_id)

        with pytest.raises(UnknownDynamicError):
            store.read_simulation_inputs(REQUEST, uk_dynamic)
        with pytest.raises(ReportSideError) as report_refusal:
            store.read_report_inputs(REQUEST, baseline=uk_dynamic, reform=uk_dynamic)
        with pytest.raises(ReportSideError) as window_refusal:
            store.read_budget_window_inputs(
                REQUEST, baseline=CURRENT_LAW, reform=uk_dynamic
            )
        uk_inputs = store.read_simulation_inputs(UK_REQUEST, uk_dynamic)

    assert report_refusal.value.side == "baseline"
    assert isinstance(report_refusal.value.error, UnknownDynamicError)
    assert window_refusal.value.side == "reform"
    assert isinstance(window_refusal.value.error, UnknownDynamicError)
    assert uk_inputs.simulation.reform == {ELASTICITY: {"2026-01-01.2200-12-31": -0.05}}


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
        # API v1 stores each name in one kind's table; a policy setting the
        # dynamic's parameter needs the name in both.
        insert_parameter(connection, economy.catalog, ELASTICITY, kind="policy")
        overlapping_policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.1, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        act_as_the_service(connection)
        store = economy_store_on(connection)

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


def test_a_reports_call_is_recorded_or_its_run_failed(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        recorded, refused = (
            insert_pending_report(connection, economy) for _ in range(2)
        )
        act_as_the_service(connection)
        store = economy_store_on(connection)

        status = store.record_report_call(recorded.report_run_id, "fc-coordinator")
        failed = store.fail_unstarted_report(
            refused.report_run_id,
            refused.simulation_run_ids,
            "modal_call_refused: no",
        )
        recorded_state = _status(connection, "report_runs", recorded.report_run_id)
        refused_state = _status(connection, "report_runs", refused.report_run_id)
        refused_baseline = _status(
            connection, "simulation_runs", refused.baseline.simulation_run_id
        )

    assert (status, failed) == (ReportRunStatus.PENDING, True)
    assert recorded_state == ("pending", "fc-coordinator")
    assert refused_state == ("failed", None)
    assert refused_baseline == ("failed", None)


def test_a_simulations_call_is_recorded_or_its_run_failed(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        pending = RunShape(status=SimulationStatus.PENDING)
        recorded, refused = (
            insert_simulation_run(connection, economy.population(), pending)
            for _ in range(2)
        )
        act_as_the_service(connection)
        store = economy_store_on(connection)

        status = store.record_simulation_call(recorded.simulation_run_id, "fc-worker")
        failed = store.fail_unstarted_simulation(
            refused.simulation_run_id, "modal_call_refused: no"
        )
        recorded_state = _status(
            connection, "simulation_runs", recorded.simulation_run_id
        )
        refused_state = _status(
            connection, "simulation_runs", refused.simulation_run_id
        )

    assert (status, failed) == (SimulationStatus.PENDING, True)
    assert recorded_state == ("pending", "fc-worker")
    assert refused_state == ("failed", None)


def test_a_report_and_a_simulation_are_read_as_they_stand(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        report = insert_pending_report(connection, insert_economy_catalog(connection))
        act_as_the_service(connection)
        store = economy_store_on(connection)
        store.record_report_call(report.report_run_id, "fc-coordinator")

        stored_report = store.read_report(report.report_id)
        stored_simulation = store.read_simulation(report.baseline.simulation_id)
        missing = (store.read_report(uuid4()), store.read_simulation(uuid4()))

    assert missing == (None, None)
    assert stored_report is not None
    assert stored_report.run is not None
    assert (stored_report.run.id, stored_report.run.status) == (
        report.report_run_id,
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
        ("reform", DynamicWithoutValuesError),
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
        # API v1 stores each name in one kind's table; a policy setting the
        # dynamic's parameter needs the name in both.
        insert_parameter(connection, economy.catalog, ELASTICITY, kind="policy")
        overlapping_policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.1, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        dynamic_without_values_id = insert_dynamic(connection, economy.catalog)
        act_as_the_service(connection)
        refused = {
            UnknownDynamicError: SimulationChoice(None, uuid4()),
            UnknownPolicyError: SimulationChoice(uuid4(), None),
            MissingPolicyParametersError: SimulationChoice(lacking_policy_id, None),
            OverlappingReformError: SimulationChoice(overlapping_policy_id, dynamic_id),
            DynamicWithoutValuesError: SimulationChoice(
                None, dynamic_without_values_id
            ),
        }[error_type]
        choices = {"baseline": CURRENT_LAW, "reform": CURRENT_LAW, side: refused}

        with pytest.raises(ReportSideError) as raised:
            economy_store_on(connection).read_report_inputs(
                REQUEST,
                baseline=choices["baseline"],
                reform=choices["reform"],
            )

    assert raised.value.side == side
    assert isinstance(raised.value.error, error_type)


def _forced_rerun() -> NewReportRerun:
    """Return a forced report re-run, whose create only inserts rows."""
    return NewReportRerun(
        report_id=uuid4(),
        run_id=uuid4(),
        population=EconomyPopulation(
            tax_benefit_model_version_id=uuid4(),
            region_id=uuid4(),
            dataset_id=uuid4(),
            year=2026,
        ),
        versions=VERSIONS,
        baseline=RerunSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=RerunSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        forced=True,
    )


def test_a_create_whose_commit_fails_may_have_been_written() -> None:
    session = RecordingSession()
    store = EconomyRunStore(lambda: CommitFailingTransaction(session))

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        store.create_report_rerun(_forced_rerun())

    assert isinstance(raised.value.__cause__, OperationalError)
    assert session.statements


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
        store.create_report_rerun(_forced_rerun())


def test_every_side_refusal_is_raised_with_its_side(monkeypatch) -> None:
    """Every error the side refusal type names is wrapped; none slips through."""
    catalog = EconomyCatalogRows(
        catalog_version_id=uuid4(),
        model_id=uuid4(),
        region=CatalogRegion(id=uuid4(), code="us", default_dataset_id=uuid4()),
        dataset=CatalogDataset(id=uuid4(), name="populace_us_2024"),
    )
    # Each patch replaces the name where its caller looks it up.
    monkeypatch.setattr(
        economy_run_store, "read_catalog_rows", lambda *_, **__: catalog
    )
    refusals = {
        UnknownPolicyError: UnknownPolicyError("no policy"),
        UnknownDynamicError: UnknownDynamicError("no dynamic"),
        MissingPolicyParametersError: MissingPolicyParametersError(["gov.a"]),
        OverlappingReformError: OverlappingReformError(["gov.a"]),
        DynamicWithoutValuesError: DynamicWithoutValuesError("no values"),
    }
    assert set(get_args(SideRefusal.__value__)) == set(refusals)

    @contextmanager
    def no_database():
        yield None

    store = EconomyRunStore(no_database)
    for refusal in refusals.values():

        def refuse(*_, refusal=refusal):
            raise refusal

        monkeypatch.setattr(economy_inputs, "read_simulation_values", refuse)
        with pytest.raises(ReportSideError) as raised:
            store.read_report_inputs(REQUEST, baseline=CURRENT_LAW, reform=CURRENT_LAW)
        assert (raised.value.side, raised.value.error) == ("baseline", refusal)


def test_a_request_naming_a_dataset_and_the_local_area_dataset_is_refused() -> None:
    """A re-run names its stored dataset and a create the local-area one, never both."""
    named = replace(UK_REQUEST, dataset_name="microcosm_uk_2024_25")

    with pytest.raises(ValueError, match="either a dataset name or"):
        read_catalog_rows(
            None,  # refused before any read
            named,
            local_area_dataset_name=LOCAL_AREA_DATASET_NAME,
        )
