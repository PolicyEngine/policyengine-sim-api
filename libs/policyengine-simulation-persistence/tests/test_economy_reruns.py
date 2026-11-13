"""Re-running a stored economy report or simulation on a later catalog version.

A re-run reads what the stored report or simulation calculates, creates a new
run of it on the current versions, and keeps every earlier run. A report's
side may reuse a run of its own simulation that succeeded on those versions;
nothing is created while the record has a run pending or running.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the simulation service's role, each
in a transaction rolled back afterwards.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_service, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    LATER_POLICYENGINE_VERSION,
    LATER_VERSIONS,
    OPTIONS,
    OUTPUT_BUCKET_PREFIX,
    VERSIONS,
    EconomyCatalog,
    ReportRunOutcome,
    RunShape,
    StoredRun,
    insert_economy_catalog,
    insert_later_catalog,
    insert_report_of,
    insert_report_run_of,
    insert_run_of,
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    EconomyPopulation,
    EconomyRecordReferenceError,
    EconomyRequest,
    EconomyRunStore,
    FoundReportRun,
    MissingPolicyParametersError,
    NewReportRerun,
    NewSimulationRerun,
    ReportDefinition,
    ReportRunStatus,
    ReportRunTrigger,
    ReportSideError,
    RerunRecordDeletedError,
    RerunSide,
    ReusableRun,
    RunReuse,
    SideDefinition,
    SimulationChoice,
    SimulationDefinition,
    SimulationStatus,
    SimulationType,
    UnknownDatasetError,
    UnknownRegionError,
    find_reusable_run,
    find_reusable_run_of,
    insert_report_rerun,
    insert_simulation_rerun,
    lock_reruns_of,
    read_report_definition,
    read_simulation_definition,
    simulations,
)
from public_api_rows import (
    StoredValue,
    insert_catalog_version,
    insert_dataset,
    insert_household,
    insert_policy,
    insert_region,
)
from sqlalchemy import bindparam, event, insert, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
# What a re-run asks the current catalog version for: the stored report's
# region and dataset, on the later .py version.
LATER_REQUEST = EconomyRequest(
    country="us",
    policyengine_version=LATER_POLICYENGINE_VERSION,
    region_code="us",
    dataset_name="populace_us_2024",
    year=2026,
)
LATER_REUSE = RunReuse(
    versions=LATER_VERSIONS, inputs=OPTIONS, output_uri_prefix=OUTPUT_BUCKET_PREFIX
)


@dataclass(frozen=True)
class StoredReport:
    """A stored report, the simulation runs its first run compared, and that run."""

    report_id: UUID
    baseline: StoredRun
    reform: StoredRun
    report_run_id: UUID


def _stored_report(
    connection: Connection,
    economy: EconomyCatalog,
    *,
    reform_choice: SimulationChoice = CURRENT_LAW,
    run_status: ReportRunStatus = ReportRunStatus.SUCCEEDED,
) -> StoredReport:
    population = economy.population()
    baseline = insert_simulation_run(connection, population)
    reform = insert_simulation_run(
        connection, population, RunShape(choice=reform_choice)
    )
    report_id = insert_report_of(connection, economy, (baseline, reform))
    report_run_id = insert_report_run_of(
        connection, report_id, (baseline, reform), ReportRunOutcome(status=run_status)
    )
    return StoredReport(
        report_id=report_id,
        baseline=baseline,
        reform=reform,
        report_run_id=report_run_id,
    )


def _row(connection: Connection, table_name: str, row_id: UUID) -> dict:
    row = connection.execute(
        text(f"SELECT * FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
        {"id": row_id},
    ).mappings()
    return dict(row.one())


def _count_runs_of(connection: Connection, simulation_id: UUID) -> int:
    return connection.execute(
        text("SELECT count(*) FROM public.simulation_runs WHERE simulation_id = :id"),
        {"id": simulation_id},
    ).scalar_one()


def _store_on(connection: Connection) -> EconomyRunStore:
    """Return a store whose transactions are savepoints of ``connection``."""

    @contextmanager
    def savepoint() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return EconomyRunStore(savepoint)


def test_a_stored_economy_reports_definition_is_read(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        stored = _stored_report(connection, economy, reform_choice=reform_choice)
        act_as_the_service(connection)

        definition = read_report_definition(connection, stored.report_id)

    assert definition == ReportDefinition(
        id=stored.report_id,
        country="us",
        kind="economy",
        year=2026,
        region_code="us",
        dataset_name="populace_us_2024",
        inputs=OPTIONS,
        baseline=SideDefinition(
            simulation_id=stored.baseline.simulation_id, choice=CURRENT_LAW
        ),
        reform=SideDefinition(
            simulation_id=stored.reform.simulation_id, choice=reform_choice
        ),
    )


def test_a_budget_window_or_household_report_is_read_as_its_kind(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        population = economy.population()
        sides = (
            insert_simulation_run(connection, population),
            insert_simulation_run(connection, population),
        )
        window_id = insert_report_of(connection, economy, sides, budget_window_size=3)
        household_id = insert_household(
            connection, country="us", default_year=2026, document={"people": {}}
        )
        household_report_id = insert_report_of(
            connection, economy, sides, household_id=household_id
        )
        act_as_the_service(connection)

        window = read_report_definition(connection, window_id)
        household = read_report_definition(connection, household_report_id)

    assert window is not None
    assert household is not None
    assert (window.kind, household.kind) == ("budget_window", "household")


def test_an_unknown_report_or_simulation_has_no_definition(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        act_as_the_service(connection)

        assert read_report_definition(connection, uuid4()) is None
        assert read_simulation_definition(connection, uuid4()) is None


def test_a_stored_economy_simulations_definition_is_read(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, country="uk")
        policy_id = insert_policy(connection, economy.catalog)
        choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        stored = insert_simulation_run(
            connection, economy.population(year=2027), RunShape(choice=choice)
        )
        act_as_the_service(connection)

        definition = read_simulation_definition(connection, stored.simulation_id)

    assert definition == SimulationDefinition(
        id=stored.simulation_id,
        country="uk",
        kind="economy",
        year=2027,
        region_code="uk",
        dataset_name="populace_uk_2024",
        choice=choice,
    )


def _find_later_run_of(
    connection: Connection,
    simulation_id: UUID,
    later: EconomyCatalog,
    *,
    excluded_run_id: UUID | None = None,
) -> ReusableRun | None:
    return find_reusable_run_of(
        connection,
        simulation_id=simulation_id,
        population=later.population(),
        versions=LATER_VERSIONS,
        inputs=OPTIONS,
        output_uri_prefix=OUTPUT_BUCKET_PREFIX,
        excluded_run_id=excluded_run_id,
    )


def _used_on_later_catalog(
    connection: Connection,
    stored: StoredReport,
    economy: EconomyCatalog,
    later: EconomyCatalog,
) -> StoredRun:
    """Insert a succeeded later run of the baseline that another report used.

    The other report, created on ``economy`` beside the stored one, compares
    the stored report's baseline simulation (reusing its run) with a reform
    simulation of its own; its initial run succeeded, and its manual re-run
    on the later catalog created both sides' later runs and succeeded. The
    stored report has no run on the later versions, so a re-run of it has
    none to join, and its reform simulation has no later run.
    """
    other_reform = insert_simulation_run(connection, economy.population())
    other_report_id = insert_report_of(
        connection, economy, (stored.baseline, other_reform)
    )
    insert_report_run_of(connection, other_report_id, (stored.baseline, other_reform))
    later_shape = RunShape(versions=LATER_VERSIONS)
    later_baseline, later_other_reform = (
        insert_run_of(connection, simulation_id, later.population(), later_shape)
        for simulation_id in (stored.baseline.simulation_id, other_reform.simulation_id)
    )
    insert_report_run_of(
        connection,
        other_report_id,
        (later_baseline, later_other_reform),
        ReportRunOutcome(versions=LATER_VERSIONS, trigger=ReportRunTrigger.MANUAL),
    )
    return later_baseline


def test_a_simulations_own_run_on_a_later_catalog_version_is_reused(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        later_baseline = _used_on_later_catalog(connection, stored, economy, later)
        act_as_the_service(connection)

        found = _find_later_run_of(connection, stored.baseline.simulation_id, later)
        excluded = _find_later_run_of(
            connection,
            stored.baseline.simulation_id,
            later,
            excluded_run_id=later_baseline.simulation_run_id,
        )
        # The run on the earlier catalog version is not on the current versions.
        earlier = find_reusable_run_of(
            connection,
            simulation_id=stored.baseline.simulation_id,
            population=economy.population(),
            versions=LATER_VERSIONS,
            inputs=OPTIONS,
            output_uri_prefix=OUTPUT_BUCKET_PREFIX,
        )

    assert found == ReusableRun(
        simulation_id=stored.baseline.simulation_id,
        simulation_run_id=later_baseline.simulation_run_id,
    )
    assert excluded is None
    assert earlier is None


def test_another_simulations_run_is_not_reused_for_a_simulations_rerun(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        _used_on_later_catalog(connection, stored, economy, later)
        other = _stored_report(connection, economy)
        act_as_the_service(connection)

        found = _find_later_run_of(connection, other.baseline.simulation_id, later)

    assert found is None


def _rerun_of(stored: StoredReport, later: EconomyCatalog) -> NewReportRerun:
    """Return an unforced re-run of ``stored`` on the later catalog's versions."""
    return NewReportRerun(
        report_id=stored.report_id,
        run_id=uuid4(),
        population=later.population(),
        versions=LATER_VERSIONS,
        baseline=RerunSide(
            simulation_id=stored.baseline.simulation_id,
            simulation_run_id=uuid4(),
            reused=False,
        ),
        reform=RerunSide(
            simulation_id=stored.reform.simulation_id,
            simulation_run_id=uuid4(),
            reused=False,
        ),
    )


def test_a_report_rerun_joins_or_creates_runs_of_the_reports_own_simulations(
    public_api_database_url,
) -> None:
    """Answers 4 and 5: a side joins its simulation's run on the versions.

    The baseline's later run is another report's; the reform has none.
    """
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        joined = _used_on_later_catalog(connection, stored, economy, later)
        rerun = _rerun_of(stored, later)
        act_as_the_service(connection)

        insert_report_rerun(connection, rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)
        reform_run = _row(connection, "simulation_runs", rerun.reform.simulation_run_id)
        earlier_run = _row(connection, "report_runs", stored.report_run_id)
        baseline_runs = _count_runs_of(connection, stored.baseline.simulation_id)

    assert {
        key: report_run[key]
        for key in (
            "report_id",
            "baseline_simulation_run_id",
            "reform_simulation_run_id",
            "country_package_version",
            "policyengine_version",
            "status",
            "trigger",
            "modal_call_id",
            "budget_window_run_id",
            "year",
        )
    } == {
        "report_id": stored.report_id,
        "baseline_simulation_run_id": joined.simulation_run_id,
        "reform_simulation_run_id": rerun.reform.simulation_run_id,
        "country_package_version": LATER_VERSIONS.country_package_version,
        "policyengine_version": LATER_VERSIONS.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "trigger": ReportRunTrigger.MANUAL,
        "modal_call_id": None,
        "budget_window_run_id": None,
        "year": None,
    }
    assert {
        key: reform_run[key]
        for key in (
            "simulation_id",
            "year",
            "tax_benefit_model_version_id",
            "dataset_id",
            "region_id",
            "country_package_version",
            "policyengine_version",
            "status",
            "modal_call_id",
        )
    } == {
        "simulation_id": stored.reform.simulation_id,
        "year": 2026,
        "tax_benefit_model_version_id": later.catalog.version_id,
        "dataset_id": later.dataset_id,
        "region_id": later.region_id,
        "country_package_version": LATER_VERSIONS.country_package_version,
        "policyengine_version": LATER_VERSIONS.policyengine_version,
        "status": SimulationStatus.PENDING,
        "modal_call_id": None,
    }
    # The earlier run is kept, and the joining side gets no new run.
    assert earlier_run["status"] == ReportRunStatus.SUCCEEDED
    assert baseline_runs == 2
    assert reform_run["forced"] is False


def _unforced_rerun_on_unchanged_versions(
    stored: StoredReport, economy: EconomyCatalog
) -> NewReportRerun:
    """Return a re-run of ``stored`` on the versions it already ran on."""
    return replace(_forced_rerun_of(stored, economy), run_id=uuid4(), forced=False)


@pytest.mark.parametrize(
    "status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.SUCCEEDED],
)
def test_a_report_rerun_joins_the_reports_run_on_its_versions(
    public_api_database_url, status: ReportRunStatus
) -> None:
    """Answer 3: a re-run joins a run under way, so #1059's 409 goes."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_report(connection, economy, run_status=status)
        rerun = _unforced_rerun_on_unchanged_versions(stored, economy)
        act_as_the_service(connection)

        joined = insert_report_rerun(connection, rerun)
        created_report_runs = connection.execute(
            text("SELECT count(*) FROM public.report_runs WHERE id = :id"),
            {"id": rerun.run_id},
        ).scalar_one()
        created_simulation_runs = connection.execute(
            text(
                "SELECT count(*) FROM public.simulation_runs WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list(rerun.new_simulation_run_ids)},
        ).scalar_one()

    assert joined == FoundReportRun(
        id=stored.report_run_id,
        inserted=False,
        status=status,
        baseline_simulation_run_id=stored.baseline.simulation_run_id,
        reform_simulation_run_id=stored.reform.simulation_run_id,
    )
    assert (created_report_runs, created_simulation_runs) == (0, 0)


def test_a_report_rerun_beside_a_run_under_way_on_older_versions_creates_its_own(
    public_api_database_url,
) -> None:
    """A run on other versions is no run to join; the re-run is for the current ones."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy, run_status=ReportRunStatus.RUNNING)
        rerun = _rerun_of(stored, later)
        act_as_the_service(connection)

        created = insert_report_rerun(connection, rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)

    assert (created.id, created.inserted, created.status) == (
        rerun.run_id,
        True,
        ReportRunStatus.PENDING,
    )
    assert report_run["policyengine_version"] == LATER_POLICYENGINE_VERSION


def test_a_report_whose_runs_have_ended_is_rerun_again(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy, run_status=ReportRunStatus.FAILED)
        first = _rerun_of(stored, later)
        act_as_the_service(connection)
        insert_report_rerun(connection, first)
        # The first re-run's run, and the simulation runs it created, fail
        # before a second re-run.
        connection.execute(text("RESET ROLE"))
        for table_name, run_ids in (
            ("report_runs", [first.run_id]),
            ("simulation_runs", list(first.new_simulation_run_ids)),
        ):
            connection.execute(
                text(
                    f"UPDATE public.{table_name} SET status = 'failed', "  # noqa: S608  # fixed table names
                    "completed_at = now() WHERE id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": run_ids},
            )
        second = _rerun_of(stored, later)
        act_as_the_service(connection)

        insert_report_rerun(connection, second)

        assert _row(connection, "report_runs", second.run_id)["status"] == (
            ReportRunStatus.PENDING
        )


def test_a_simulation_rerun_joins_its_run_on_the_current_versions_until_it_fails(
    public_api_database_url,
) -> None:
    """Answer 3: a re-run joins a run under way or succeeded; 2: never a failed one."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = insert_simulation_run(connection, economy.population())
        rerun = NewSimulationRerun(
            simulation_id=stored.simulation_id,
            run_id=uuid4(),
            population=later.population(),
            versions=LATER_VERSIONS,
        )
        act_as_the_service(connection)

        inserted = insert_simulation_rerun(connection, rerun)
        created = _row(connection, "simulation_runs", rerun.run_id)
        joined_under_way = insert_simulation_rerun(
            connection, replace(rerun, run_id=uuid4())
        )
        joined_succeeded = insert_simulation_rerun(
            connection,
            NewSimulationRerun(
                simulation_id=stored.simulation_id,
                run_id=uuid4(),
                population=economy.population(),
                versions=VERSIONS,
            ),
        )
        connection.execute(text("RESET ROLE"))
        connection.execute(
            text(
                "UPDATE public.simulation_runs SET status = 'failed', "
                "completed_at = now() WHERE id = :id"
            ),
            {"id": rerun.run_id},
        )
        act_as_the_service(connection)
        after_failure = insert_simulation_rerun(
            connection, replace(rerun, run_id=uuid4())
        )
        runs_of_the_simulation = _count_runs_of(connection, stored.simulation_id)

    assert (inserted.id, inserted.inserted, inserted.status) == (
        rerun.run_id,
        True,
        SimulationStatus.PENDING,
    )
    assert (
        created["simulation_id"],
        created["tax_benefit_model_version_id"],
        created["policyengine_version"],
        created["status"],
        created["forced"],
    ) == (
        stored.simulation_id,
        later.catalog.version_id,
        LATER_POLICYENGINE_VERSION,
        SimulationStatus.PENDING,
        False,
    )
    assert (joined_under_way.id, joined_under_way.inserted) == (rerun.run_id, False)
    assert (
        joined_succeeded.id,
        joined_succeeded.inserted,
        joined_succeeded.status,
    ) == (stored.simulation_run_id, False, SimulationStatus.SUCCEEDED)
    assert after_failure.inserted is True
    assert after_failure.id not in {rerun.run_id, stored.simulation_run_id}
    assert runs_of_the_simulation == 3


def test_a_rerun_holds_its_records_lock_until_its_transaction_ends(
    public_api_database_url,
) -> None:
    """A second re-run of the record waits; one of another record does not."""
    with rolled_back_transaction(public_api_database_url) as holder:
        economy = insert_economy_catalog(holder)
        later = insert_later_catalog(holder, economy)
        stored = _stored_report(holder, economy, run_status=ReportRunStatus.FAILED)
        act_as_the_service(holder)
        insert_report_rerun(holder, _rerun_of(stored, later))

        with rolled_back_transaction(public_api_database_url) as waiter:
            act_as_the_service(waiter)
            waiter.execute(text("SET LOCAL lock_timeout = '200ms'"))
            lock_reruns_of(waiter, uuid4())
            with pytest.raises(OperationalError, match="lock timeout"):
                lock_reruns_of(waiter, stored.report_id)


def test_a_report_reruns_inputs_are_read_on_the_current_catalog_version(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
        later = insert_later_catalog(connection, economy, parameter_names=(CTC_AMOUNT,))
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        stored = _stored_report(connection, economy, reform_choice=reform_choice)
        reused = _used_on_later_catalog(connection, stored, economy, later)
        act_as_the_service(connection)
        store = _store_on(connection)
        sides = {
            "baseline": SideDefinition(stored.baseline.simulation_id, CURRENT_LAW),
            "reform": SideDefinition(stored.reform.simulation_id, reform_choice),
        }

        inputs = store.read_report_rerun_inputs(
            LATER_REQUEST, **sides, reuse=LATER_REUSE
        )
        forced = store.read_report_rerun_inputs(LATER_REQUEST, **sides, reuse=None)

    assert (inputs.catalog.catalog_version_id, inputs.catalog.region.id) == (
        later.catalog.version_id,
        later.region_id,
    )
    assert inputs.catalog.dataset.id == later.dataset_id
    assert inputs.baseline.reusable_run == ReusableRun(
        simulation_id=stored.baseline.simulation_id,
        simulation_run_id=reused.simulation_run_id,
    )
    assert inputs.reform.reusable_run is None
    assert inputs.reform.reform == {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
    assert forced.baseline.reusable_run is None


def test_both_sides_of_one_simulation_never_reuse_the_same_run(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        reused = _used_on_later_catalog(connection, stored, economy, later)
        act_as_the_service(connection)
        side = SideDefinition(stored.baseline.simulation_id, CURRENT_LAW)

        inputs = _store_on(connection).read_report_rerun_inputs(
            LATER_REQUEST, baseline=side, reform=side, reuse=LATER_REUSE
        )

    assert inputs.baseline.reusable_run == ReusableRun(
        simulation_id=stored.baseline.simulation_id,
        simulation_run_id=reused.simulation_run_id,
    )
    assert inputs.reform.reusable_run is None


def test_a_rerun_names_the_side_whose_policy_the_current_catalog_cannot_set(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
        insert_later_catalog(connection, economy)
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        stored = _stored_report(connection, economy, reform_choice=reform_choice)
        act_as_the_service(connection)

        with pytest.raises(ReportSideError) as refused:
            _store_on(connection).read_report_rerun_inputs(
                LATER_REQUEST,
                baseline=SideDefinition(stored.baseline.simulation_id, CURRENT_LAW),
                reform=SideDefinition(stored.reform.simulation_id, reform_choice),
                reuse=None,
            )

    assert refused.value.side == "reform"
    assert isinstance(refused.value.error, MissingPolicyParametersError)
    assert refused.value.error.parameter_names == (CTC_AMOUNT,)


@pytest.mark.parametrize(
    ("missing", "expected"),
    [("region", UnknownRegionError), ("dataset", UnknownDatasetError)],
)
def test_a_rerun_is_refused_when_the_current_catalog_lacks_its_region_or_dataset(
    public_api_database_url, missing: str, expected: type[Exception]
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        other_dataset_id = insert_dataset(connection, later.catalog, "acs_2024")
        insert_region(
            connection,
            later.catalog,
            "state/ca",
            default_dataset_id=other_dataset_id,
            region_type="state",
        )
        stored = _stored_report(connection, economy)
        act_as_the_service(connection)
        request = (
            replace(LATER_REQUEST, region_code="state/tx")
            if missing == "region"
            else replace(LATER_REQUEST, dataset_name="populace_us_2023")
        )

        with pytest.raises(expected):
            _store_on(connection).read_report_rerun_inputs(
                request,
                baseline=SideDefinition(stored.baseline.simulation_id, CURRENT_LAW),
                reform=SideDefinition(stored.reform.simulation_id, CURRENT_LAW),
                reuse=None,
            )


def test_a_simulation_reruns_inputs_are_read_on_the_current_catalog_version(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
        later = insert_later_catalog(connection, economy, parameter_names=(CTC_AMOUNT,))
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
        act_as_the_service(connection)

        # A simulation's re-run reads the current catalog version as a new one's.
        inputs = _store_on(connection).read_simulation_inputs(LATER_REQUEST, choice)

    assert inputs.catalog.catalog_version_id == later.catalog.version_id
    assert inputs.simulation.choice == choice
    assert inputs.simulation.reform == {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}


def test_a_rerun_and_its_runs_are_created_through_the_store(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        rerun = _rerun_of(stored, later)
        simulation_rerun = NewSimulationRerun(
            simulation_id=insert_simulation_run(
                connection, economy.population()
            ).simulation_id,
            run_id=uuid4(),
            population=later.population(),
            versions=LATER_VERSIONS,
        )
        act_as_the_service(connection)
        store = _store_on(connection)

        store.create_report_rerun(rerun)
        store.create_simulation_rerun(simulation_rerun)
        definition = store.read_report_definition(stored.report_id)
        simulation = store.read_simulation_definition(simulation_rerun.simulation_id)
        # A second re-run joins the first's run, which is still pending.
        joined = store.create_report_rerun(_rerun_of(stored, later))
        assert (joined.id, joined.inserted) == (rerun.run_id, False)

        assert _row(connection, "report_runs", rerun.run_id)["trigger"] == (
            ReportRunTrigger.MANUAL
        )
        assert _row(connection, "simulation_runs", simulation_rerun.run_id)[
            "status"
        ] == (SimulationStatus.PENDING)
    assert definition is not None
    assert definition.id == stored.report_id
    assert simulation is not None
    assert simulation.id == simulation_rerun.simulation_id


def test_a_run_on_another_catalog_version_is_not_reused_by_a_new_report(
    public_api_database_url,
) -> None:
    """Only the run's own catalog version differs from the request's.

    It is also the newest run, so it would be found without that condition.
    """
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy)
        connection.execute(
            text(
                "UPDATE public.simulation_runs "
                "SET tax_benefit_model_version_id = :later, "
                "completed_at = completed_at + interval '1 day' WHERE id = :id"
            ),
            {
                "later": later.catalog.version_id,
                "id": stored.baseline.simulation_run_id,
            },
        )
        act_as_the_service(connection)

        found = find_reusable_run(
            connection,
            choice=CURRENT_LAW,
            population=economy.population(),
            versions=VERSIONS,
            inputs=OPTIONS,
            output_uri_prefix=OUTPUT_BUCKET_PREFIX,
        )

    # The reform's run, the same simulation definition on the right catalog
    # version, is the only one left to reuse.
    assert found == ReusableRun(
        simulation_id=stored.reform.simulation_id,
        simulation_run_id=stored.reform.simulation_run_id,
    )


def test_a_household_or_filtered_simulation_and_another_model_are_read_as_such(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        household_id = insert_household(
            connection, country="us", default_year=2026, document={"people": {}}
        )
        household_simulation_id = uuid4()
        filtered_simulation_id = uuid4()
        connection.execute(
            insert(simulations).values(
                id=household_simulation_id,
                simulation_type=SimulationType.HOUSEHOLD,
                household_id=household_id,
                tax_benefit_model_version_id=economy.catalog.version_id,
                year=2026,
            )
        )
        connection.execute(
            insert(simulations).values(
                id=filtered_simulation_id,
                simulation_type=SimulationType.ECONOMY,
                dataset_id=economy.dataset_id,
                region_id=economy.region_id,
                tax_benefit_model_version_id=economy.catalog.version_id,
                year=2026,
                filter_field="state_code",
                filter_value="CA",
            )
        )
        other_model_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO public.tax_benefit_models (id, name) VALUES (:id, :name)"
            ),
            {"id": other_model_id, "name": "another-model"},
        )
        other = _insert_catalog_of(connection, other_model_id)
        other_simulation = insert_simulation_run(connection, other.population())
        act_as_the_service(connection)

        household = read_simulation_definition(connection, household_simulation_id)
        filtered = read_simulation_definition(connection, filtered_simulation_id)
        of_another_model = read_simulation_definition(
            connection, other_simulation.simulation_id
        )

    assert household is not None
    assert (household.kind, household.country) == ("household", "us")
    assert (household.region_code, household.dataset_name) == (None, None)
    assert filtered is not None
    assert filtered.kind == "unsupported"
    assert of_another_model is not None
    assert (of_another_model.kind, of_another_model.country) == ("economy", None)


def _insert_catalog_of(connection: Connection, model_id: UUID) -> EconomyCatalog:
    catalog = insert_catalog_version(
        connection,
        country="xx",
        model_id=model_id,
        policyengine_version=LATER_POLICYENGINE_VERSION,
    )
    dataset_id = insert_dataset(connection, catalog, "another_dataset")
    region_id = insert_region(
        connection, catalog, "everywhere", default_dataset_id=dataset_id
    )
    return EconomyCatalog(catalog=catalog, dataset_id=dataset_id, region_id=region_id)


def test_a_report_of_another_type_is_read_as_unsupported(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_report(connection, economy)
        connection.execute(
            text("UPDATE public.reports SET type = 'custom_total' WHERE id = :id"),
            {"id": stored.report_id},
        )
        act_as_the_service(connection)

        definition = read_report_definition(connection, stored.report_id)

    assert definition is not None
    assert definition.kind == "unsupported"


def test_a_rerun_of_a_deleted_report_or_simulation_says_the_record_is_gone(
    public_api_database_url,
) -> None:
    """The record's own key refuses the new run: the record was deleted."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        later = insert_later_catalog(connection, economy)
        stored = _stored_report(connection, economy, run_status=ReportRunStatus.FAILED)
        act_as_the_service(connection)

        with pytest.raises(RerunRecordDeletedError), connection.begin_nested():
            insert_report_rerun(
                connection, replace(_rerun_of(stored, later), report_id=uuid4())
            )
        with pytest.raises(RerunRecordDeletedError), connection.begin_nested():
            insert_simulation_rerun(
                connection,
                NewSimulationRerun(
                    simulation_id=uuid4(),
                    run_id=uuid4(),
                    population=later.population(),
                    versions=LATER_VERSIONS,
                ),
            )
        # A report whose side simulation is gone names that simulation instead.
        gone_side = replace(
            _rerun_of(stored, later),
            reform=RerunSide(
                simulation_id=uuid4(), simulation_run_id=uuid4(), reused=False
            ),
        )
        with (
            pytest.raises(EconomyRecordReferenceError) as refused,
            connection.begin_nested(),
        ):
            insert_report_rerun(connection, gone_side)

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulation_runs",
        "simulation_id",
    )


def _forced_rerun_of(stored: StoredReport, economy: EconomyCatalog) -> NewReportRerun:
    """Return a forced re-run of ``stored`` on the versions it already ran on."""
    return NewReportRerun(
        report_id=stored.report_id,
        run_id=uuid4(),
        population=economy.population(),
        versions=VERSIONS,
        baseline=RerunSide(
            simulation_id=stored.baseline.simulation_id,
            simulation_run_id=uuid4(),
            reused=False,
        ),
        reform=RerunSide(
            simulation_id=stored.reform.simulation_id,
            simulation_run_id=uuid4(),
            reused=False,
        ),
        forced=True,
    )


@pytest.mark.parametrize(
    "status",
    [ReportRunStatus.PENDING, ReportRunStatus.RUNNING, ReportRunStatus.SUCCEEDED],
)
def test_a_forced_report_rerun_is_an_extra_run_beside_any_run_on_its_versions(
    public_api_database_url, status: ReportRunStatus
) -> None:
    """Answer 3: forced runs are marked, never refused and never conflict."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = _stored_report(connection, economy, run_status=status)
        rerun = _forced_rerun_of(stored, economy)
        act_as_the_service(connection)

        insert_report_rerun(connection, rerun)
        report_run = _row(connection, "report_runs", rerun.run_id)
        simulation_runs = [
            _row(connection, "simulation_runs", run_id)
            for run_id in rerun.new_simulation_run_ids
        ]
        earlier_run = _row(connection, "report_runs", stored.report_run_id)

    assert (report_run["forced"], report_run["trigger"], report_run["status"]) == (
        True,
        ReportRunTrigger.MANUAL,
        ReportRunStatus.PENDING,
    )
    assert len(simulation_runs) == 2
    assert all(run["forced"] for run in simulation_runs)
    assert all(run["status"] == SimulationStatus.PENDING for run in simulation_runs)
    assert earlier_run["status"] == status
    assert earlier_run["forced"] is False


@pytest.mark.parametrize("forced", [True, False])
def test_a_report_rerun_names_no_reused_run(forced: bool) -> None:  # noqa: FBT001  # the parameter
    """Each side joins or creates its run; a forced one creates it (answer 3)."""
    reused = RerunSide(simulation_id=uuid4(), simulation_run_id=uuid4(), reused=True)
    created = RerunSide(simulation_id=uuid4(), simulation_run_id=uuid4(), reused=False)

    with pytest.raises(ValueError, match="reusing none"):
        NewReportRerun(
            report_id=uuid4(),
            run_id=uuid4(),
            population=EconomyPopulation(
                tax_benefit_model_version_id=uuid4(),
                region_id=uuid4(),
                dataset_id=uuid4(),
                year=2026,
            ),
            versions=VERSIONS,
            baseline=reused,
            reform=created,
            forced=forced,
        )


def test_a_report_rerun_losing_to_a_run_created_after_its_lookup_joins_it(
    committed_engine, committed_catalog
) -> None:
    """It joins that run and takes back the simulation runs it had inserted."""
    economy = committed_catalog
    failed = RunShape(status=SimulationStatus.FAILED)
    with committed_engine.begin() as connection:
        baseline = insert_simulation_run(connection, economy.population(), failed)
        reform = insert_simulation_run(connection, economy.population(), failed)
        report_id = insert_report_of(connection, economy, (baseline, reform))
    stored = StoredReport(
        report_id=report_id,
        baseline=baseline,
        reform=reform,
        report_run_id=uuid4(),
    )
    rerun = _unforced_rerun_on_unchanged_versions(stored, economy)
    competing_run_ids: list[UUID] = []

    def create_a_competing_run_first(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if competing_run_ids or not statement.lstrip().startswith(
            "INSERT INTO public.report_runs"
        ):
            return
        with committed_engine.begin() as other:
            competing_run_ids.append(
                insert_report_run_of(
                    other,
                    report_id,
                    (baseline, reform),
                    ReportRunOutcome(status=ReportRunStatus.PENDING),
                )
            )

    with committed_engine.begin() as connection:
        act_as_the_service(connection)
        event.listen(connection, "before_cursor_execute", create_a_competing_run_first)
        try:
            joined = insert_report_rerun(connection, rerun)
        finally:
            event.remove(
                connection, "before_cursor_execute", create_a_competing_run_first
            )
        kept = connection.execute(
            text(
                "SELECT count(*) FROM public.simulation_runs WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
            {"ids": list(rerun.new_simulation_run_ids)},
        ).scalar_one()

    assert (joined.id, joined.inserted) == (competing_run_ids[0], False)
    assert kept == 0
