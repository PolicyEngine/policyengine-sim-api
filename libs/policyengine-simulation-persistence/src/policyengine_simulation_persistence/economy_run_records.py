"""The simulation service's reads and writes for economy simulations and reports.

An economy report compares a baseline and a reform simulation of one
region's data for one year. The service reads the catalog rows the request
names from the Public API's tables, then creates in one transaction:

- for each side, a new ``simulations`` row and its first ``simulation_runs``
  row, ``pending`` with no Modal call ID; or, when a run of the same
  simulation already succeeded and may be reused (``find_reusable_run``),
  nothing: the report names that run and its simulation instead;
- the ``reports`` row, naming both simulations and the reform's policy, with
  the report's options in ``inputs``;
- its first ``report_runs`` row, ``pending`` with no Modal call ID and
  trigger ``initial``, naming both simulation runs.

A standalone economy simulation is one ``simulations`` row and its first
run, created the same way.

The service then starts the Modal call that calculates the run it created,
and records the call's ID on that run, or fails it at once when Modal
refuses the call. Both updates apply only while the run is still
``pending`` with no call ID: the coordinator or worker may already have
claimed it, and the stuck-run sweep fails a pending run no call was
recorded for. Failing a report run also fails the simulation runs created
for it, under the same condition. The coordinator and workers write the
rest: ``running``, the results, and ``succeeded`` or ``failed``.

Every function takes a connection, so the caller chooses the transactions.
Each update sets only the columns the service's role may update, with
``updated_at``, whose automatic update the Public API's models make on their
own side only.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    cast,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    datasets,
    regions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    ReportRunTrigger,
    SimulationStatus,
    SimulationType,
    budget_window_runs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, FromClause, Table

# PostgreSQL's SQLSTATE for a row naming one that does not exist.
FOREIGN_KEY_VIOLATION: Final = "23503"
# A report's type, as the Public API reads it.
ECONOMY_REPORT_TYPE: Final = "economy"
# The start of the error message of a report run whose coordinator refused a
# reused simulation run's output (``reused_simulation_<reason>: <summary>``).
REFUSED_REUSE_PREFIX: Final = "reused_simulation_"
# Both run tables' statuses use these values.
_PENDING: Final = SimulationStatus.PENDING.value
_FAILED: Final = SimulationStatus.FAILED.value
# Each foreign key of the tables the service writes, by name: its table and
# the first column it constrains.
_KEY_COLUMNS: Final = {
    str(constraint.name): (table.name, constraint.column_keys[0])
    for table in (
        simulations,
        simulation_runs,
        reports,
        report_runs,
        budget_window_runs,
    )
    for constraint in table.foreign_key_constraints
}


@dataclass(frozen=True)
class CatalogRegion:
    """A region of a catalog version, and the dataset it uses by default."""

    id: UUID
    code: str
    default_dataset_id: UUID


@dataclass(frozen=True)
class CatalogDataset:
    """A dataset of a catalog version."""

    id: UUID
    name: str


@dataclass(frozen=True)
class RunVersions:
    """The country package and .py versions a run calculates on."""

    country_package_version: str
    policyengine_version: str


@dataclass(frozen=True)
class EconomyPopulation:
    """What every simulation of one request calculates on.

    The catalog version, the region and dataset of that version, and the
    year.
    """

    tax_benefit_model_version_id: UUID
    region_id: UUID
    dataset_id: UUID
    year: int


@dataclass(frozen=True)
class SimulationChoice:
    """A simulation's stored policy and dynamic; neither means current law."""

    policy_id: UUID | None
    dynamic_id: UUID | None


@dataclass(frozen=True)
class ReusableRun:
    """A succeeded simulation run a new report may name, and its simulation."""

    simulation_id: UUID
    simulation_run_id: UUID


@dataclass(frozen=True)
class ReportSimulation:
    """The simulation and run one side of a report names.

    With ``reused``, both exist already and the run has succeeded, so nothing
    is created for the side. Otherwise both are created, the run pending.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    choice: SimulationChoice
    reused: bool


@dataclass(frozen=True)
class NewEconomyReport:
    """An economy report to create, with its first run.

    ``inputs`` holds the report's options as the Public API reads them.
    """

    id: UUID
    run_id: UUID
    country: str
    label: str
    description: str | None
    tax_benefit_model_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    inputs: dict[str, Any]
    baseline: ReportSimulation
    reform: ReportSimulation

    @property
    def new_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the report."""
        return tuple(
            side.simulation_run_id
            for side in (self.baseline, self.reform)
            if not side.reused
        )


@dataclass(frozen=True)
class NewEconomySimulation:
    """A standalone economy simulation to create, with its first run."""

    id: UUID
    run_id: UUID
    choice: SimulationChoice
    population: EconomyPopulation
    versions: RunVersions


class EconomyRecordReferenceError(LookupError):
    """The database refused a row naming one that does not exist.

    ``table_name`` and ``column_name`` name the column whose foreign key
    refused it; both are None when the database named a key the service's
    tables don't have.
    """

    def __init__(self, table_name: str | None, column_name: str | None) -> None:
        """Keep the table and column whose key refused the row."""
        self.table_name = table_name
        self.column_name = column_name
        named = (
            "a row" if table_name is None else f"the {table_name} row's {column_name}"
        )
        super().__init__(f"{named} names no stored row")


def read_model_id(connection: Connection, *, country: str) -> UUID | None:
    """Return the id of the country's tax-benefit model, or None."""
    return connection.execute(
        select(tax_benefit_models.c.id).where(
            tax_benefit_models.c.name == f"{MODEL_NAME_PREFIX}{country}"
        )
    ).scalar_one_or_none()


def read_region(
    connection: Connection, *, catalog_version_id: UUID, region_code: str
) -> CatalogRegion | None:
    """Return the catalog version's region with this code, or None."""
    row = connection.execute(
        select(regions.c.id, regions.c.code, regions.c.default_dataset_id).where(
            regions.c.tax_benefit_model_version_id == catalog_version_id,
            regions.c.code == region_code,
        )
    ).one_or_none()
    if row is None:
        return None
    return CatalogRegion(
        id=row.id, code=row.code, default_dataset_id=row.default_dataset_id
    )


def read_dataset(
    connection: Connection, *, catalog_version_id: UUID, name: str
) -> CatalogDataset | None:
    """Return the catalog version's dataset with this name, or None."""
    return _dataset(connection, catalog_version_id, datasets.c.name == name)


def read_dataset_by_id(
    connection: Connection, *, catalog_version_id: UUID, dataset_id: UUID
) -> CatalogDataset | None:
    """Return the catalog version's dataset with this id, or None."""
    return _dataset(connection, catalog_version_id, datasets.c.id == dataset_id)


def _dataset(
    connection: Connection, catalog_version_id: UUID, condition: ColumnElement[bool]
) -> CatalogDataset | None:
    row = connection.execute(
        select(datasets.c.id, datasets.c.name).where(
            datasets.c.tax_benefit_model_version_id == catalog_version_id, condition
        )
    ).one_or_none()
    return None if row is None else CatalogDataset(id=row.id, name=row.name)


def find_reusable_run(  # noqa: PLR0913  # each keyword is one part of the match
    connection: Connection,
    *,
    choice: SimulationChoice,
    population: EconomyPopulation,
    versions: RunVersions,
    inputs: dict[str, Any],
    output_uri_prefix: str,
    excluded_run_id: UUID | None = None,
) -> ReusableRun | None:
    """Return the succeeded run a new report's simulation may name, or None.

    The run is the newest, by completion, of a run:

    - of an economy simulation with the same policy, dynamic, catalog
      version, dataset, region and year;
    - that succeeded on the same country package and .py versions, for that
      catalog version, year, dataset and region;
    - whose output file is under ``output_uri_prefix``, the output bucket,
      which never deletes a file;
    - that a succeeded report run used, in a report with the same ``inputs``
      (options), so its file holds the outputs a report with those options
      planned;
    - that no report run has refused to reuse (an error message starting
      ``reused_simulation_``), so a submission after a refused reuse
      calculates the simulation again;
    - that wasn't forced: a forced re-run's run is never named by another
      report run (C1 · 24's answer 3); and
    - that is not ``excluded_run_id``, the other side's run.

    The coordinator may still refuse it, when the new report plans outputs
    that report did not, as for a reform that turns on labour supply
    responses: the refusal fails that report run, and the run is not offered
    again.
    """
    return _newest_reusable_run(
        connection,
        (
            simulations.c.simulation_type == SimulationType.ECONOMY,
            simulations.c.policy_id.is_not_distinct_from(choice.policy_id),
            simulations.c.dynamic_id.is_not_distinct_from(choice.dynamic_id),
            simulations.c.tax_benefit_model_version_id
            == population.tax_benefit_model_version_id,
            simulations.c.dataset_id == population.dataset_id,
            simulations.c.region_id == population.region_id,
            simulations.c.year == population.year,
            simulations.c.filter_field.is_(None),
        ),
        _WantedRun(
            population=population,
            versions=versions,
            inputs=inputs,
            output_uri_prefix=output_uri_prefix,
            excluded_run_id=excluded_run_id,
        ),
    )


def find_reusable_run_of(  # noqa: PLR0913  # the simulation, then each part of the match
    connection: Connection,
    *,
    simulation_id: UUID,
    population: EconomyPopulation,
    versions: RunVersions,
    inputs: dict[str, Any],
    output_uri_prefix: str,
    excluded_run_id: UUID | None = None,
) -> ReusableRun | None:
    """Return the succeeded run of one simulation a re-run of its report may name.

    The run is the newest, by completion, of the simulation's runs that meets
    every condition ``find_reusable_run`` sets on a run, or None. Its catalog
    version, dataset and region are compared on the run itself: a re-run on
    a later .py version calculates the simulation on that version's catalog
    rows, while the simulation keeps the ones it was created on.
    """
    return _newest_reusable_run(
        connection,
        (simulation_runs.c.simulation_id == simulation_id,),
        _WantedRun(
            population=population,
            versions=versions,
            inputs=inputs,
            output_uri_prefix=output_uri_prefix,
            excluded_run_id=excluded_run_id,
        ),
    )


@dataclass(frozen=True)
class _WantedRun:
    """What a reusable run must have calculated, and the run it must not be."""

    population: EconomyPopulation
    versions: RunVersions
    inputs: dict[str, Any]
    output_uri_prefix: str
    excluded_run_id: UUID | None


def _newest_reusable_run(
    connection: Connection,
    simulation_conditions: tuple[ColumnElement[bool], ...],
    wanted: _WantedRun,
) -> ReusableRun | None:
    """Return the newest run meeting the conditions and every run condition."""
    used_by = report_runs.alias("used_by_report_run")
    refused_by = report_runs.alias("refused_by_report_run")
    used_with_same_options = (
        select(used_by.c.id)
        .join(reports, reports.c.id == used_by.c.report_id)
        .where(
            used_by.c.status == ReportRunStatus.SUCCEEDED,
            _names_run(used_by),
            cast(reports.c.inputs, JSONB) == literal(wanted.inputs, JSONB),
        )
        .exists()
    )
    refused = (
        select(refused_by.c.id)
        .where(
            refused_by.c.status == ReportRunStatus.FAILED,
            _names_run(refused_by),
            refused_by.c.error_message.startswith(
                REFUSED_REUSE_PREFIX, autoescape=True
            ),
        )
        .exists()
    )
    population = wanted.population
    versions = wanted.versions
    conditions = [
        *simulation_conditions,
        simulation_runs.c.status == SimulationStatus.SUCCEEDED,
        simulation_runs.c.forced.is_(False),
        simulation_runs.c.year == population.year,
        simulation_runs.c.tax_benefit_model_version_id
        == population.tax_benefit_model_version_id,
        simulation_runs.c.country_package_version == versions.country_package_version,
        simulation_runs.c.policyengine_version == versions.policyengine_version,
        simulation_runs.c.dataset_id == population.dataset_id,
        simulation_runs.c.region_id == population.region_id,
        simulation_runs.c.output_uri.startswith(
            wanted.output_uri_prefix, autoescape=True
        ),
        simulation_runs.c.output_sha256.is_not(None),
        used_with_same_options,
        ~refused,
    ]
    if wanted.excluded_run_id is not None:
        conditions.append(simulation_runs.c.id != wanted.excluded_run_id)
    row = connection.execute(
        select(simulation_runs.c.id, simulation_runs.c.simulation_id)
        .join(simulations, simulations.c.id == simulation_runs.c.simulation_id)
        .where(*conditions)
        .order_by(simulation_runs.c.completed_at.desc(), simulation_runs.c.id.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    return ReusableRun(simulation_id=row.simulation_id, simulation_run_id=row.id)


def _names_run(report_run: FromClause) -> ColumnElement[bool]:
    """Return whether ``report_run`` names the outer query's simulation run."""
    return or_(
        report_run.c.baseline_simulation_run_id == simulation_runs.c.id,
        report_run.c.reform_simulation_run_id == simulation_runs.c.id,
    )


def insert_economy_report(connection: Connection, report: NewEconomyReport) -> None:
    """Create the report, its first run and each side's new simulation and run.

    Raise EconomyRecordReferenceError when the database refuses a row it
    names. The refusal ends the caller's transaction, or its savepoint.
    """
    with references_checked():
        for side in (report.baseline, report.reform):
            if not side.reused:
                insert_simulation_with_run(
                    connection,
                    simulation_id=side.simulation_id,
                    run_id=side.simulation_run_id,
                    choice=side.choice,
                    population=report.population,
                    versions=report.versions,
                )
        population = report.population
        connection.execute(
            insert(reports).values(
                id=report.id,
                label=report.label,
                description=report.description,
                country=report.country,
                type=ECONOMY_REPORT_TYPE,
                user_id=None,
                tax_benefit_model_id=report.tax_benefit_model_id,
                policy_id=report.reform.choice.policy_id,
                baseline_simulation_id=report.baseline.simulation_id,
                reform_simulation_id=report.reform.simulation_id,
                household_id=None,
                dataset_id=population.dataset_id,
                region_id=population.region_id,
                year=population.year,
                budget_window_size=None,
                inputs=report.inputs,
            )
        )
        connection.execute(
            insert(report_runs).values(
                id=report.run_id,
                report_id=report.id,
                baseline_simulation_run_id=report.baseline.simulation_run_id,
                reform_simulation_run_id=report.reform.simulation_run_id,
                budget_window_run_id=None,
                year=None,
                country_package_version=report.versions.country_package_version,
                policyengine_version=report.versions.policyengine_version,
                status=ReportRunStatus.PENDING,
                trigger=ReportRunTrigger.INITIAL,
                modal_call_id=None,
            )
        )


def insert_economy_simulation(
    connection: Connection, simulation: NewEconomySimulation
) -> None:
    """Create the standalone simulation and its first run, pending.

    Raise EconomyRecordReferenceError when the database refuses a row it
    names. The refusal ends the caller's transaction, or its savepoint.
    """
    with references_checked():
        insert_simulation_with_run(
            connection,
            simulation_id=simulation.id,
            run_id=simulation.run_id,
            choice=simulation.choice,
            population=simulation.population,
            versions=simulation.versions,
        )


def insert_simulation_with_run(  # noqa: PLR0913  # the simulation's and its run's parts
    connection: Connection,
    *,
    simulation_id: UUID,
    run_id: UUID,
    choice: SimulationChoice,
    population: EconomyPopulation,
    versions: RunVersions,
) -> None:
    """Create an economy simulation and its first run, pending with no call."""
    connection.execute(
        insert(simulations).values(
            id=simulation_id,
            simulation_type=SimulationType.ECONOMY,
            dataset_id=population.dataset_id,
            household_id=None,
            policy_id=choice.policy_id,
            dynamic_id=choice.dynamic_id,
            tax_benefit_model_version_id=population.tax_benefit_model_version_id,
            region_id=population.region_id,
            year=population.year,
        )
    )
    insert_simulation_run(
        connection,
        run_id=run_id,
        simulation_id=simulation_id,
        population=population,
        versions=versions,
    )


def insert_simulation_run(  # noqa: PLR0913  # the run, what it calculates on, and its marker
    connection: Connection,
    *,
    run_id: UUID,
    simulation_id: UUID,
    population: EconomyPopulation,
    versions: RunVersions,
    forced: bool = False,
) -> None:
    """Create a run of a stored simulation on ``population``, pending with no call.

    The run records the catalog version, dataset and region it calculates
    on, which a re-run's may differ from its simulation's. ``forced`` marks
    a forced re-run's run, which nothing joins.
    """
    connection.execute(
        insert(simulation_runs).values(
            simulation_run_row(
                run_id=run_id,
                simulation_id=simulation_id,
                population=population,
                versions=versions,
                forced=forced,
            )
        )
    )


def simulation_run_row(
    *,
    run_id: UUID,
    simulation_id: UUID,
    population: EconomyPopulation,
    versions: RunVersions,
    forced: bool = False,
) -> dict[str, Any]:
    """Return an economy simulation run, pending with no call, as inserted."""
    return {
        "id": run_id,
        "simulation_id": simulation_id,
        "year": population.year,
        "tax_benefit_model_version_id": population.tax_benefit_model_version_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "dataset_id": population.dataset_id,
        "region_id": population.region_id,
        "status": SimulationStatus.PENDING,
        "modal_call_id": None,
        "forced": forced,
    }


@contextmanager
def references_checked() -> Iterator[None]:
    """Turn a foreign-key refusal into EconomyRecordReferenceError.

    Any other integrity error propagates unchanged.
    """
    try:
        yield
    except IntegrityError as error:
        driver_error = error.orig
        if getattr(driver_error, "sqlstate", None) != FOREIGN_KEY_VIOLATION:
            raise
        constraint_name = getattr(
            getattr(driver_error, "diag", None), "constraint_name", None
        )
        table_name, column_name = _KEY_COLUMNS.get(str(constraint_name), (None, None))
        raise EconomyRecordReferenceError(table_name, column_name) from None


def _pending_without_call(
    table: Table, run_id: UUID
) -> tuple[ColumnElement[bool], ...]:
    return (
        table.c.id == run_id,
        table.c.status == _PENDING,
        table.c.modal_call_id.is_(None),
    )


def record_report_run_call(
    connection: Connection, *, report_run_id: UUID, modal_call_id: str
) -> ReportRunStatus | None:
    """Record the report run's Modal call ID while it is pending with none.

    Return the run's status afterwards, or None when there is no such run. A
    run the coordinator has claimed already keeps its claim.
    """
    status = _record_call(connection, report_runs, report_run_id, modal_call_id)
    return None if status is None else ReportRunStatus(status)


def record_simulation_run_call(
    connection: Connection, *, simulation_run_id: UUID, modal_call_id: str
) -> SimulationStatus | None:
    """Record the simulation run's Modal call ID while it is pending with none.

    Return the run's status afterwards, or None when there is no such run. A
    run the worker has claimed already keeps its claim.
    """
    status = _record_call(connection, simulation_runs, simulation_run_id, modal_call_id)
    return None if status is None else SimulationStatus(status)


def _record_call(
    connection: Connection, table: Table, run_id: UUID, modal_call_id: str
) -> str | None:
    recorded = connection.execute(
        update(table)
        .where(*_pending_without_call(table, run_id))
        .values(modal_call_id=modal_call_id, updated_at=func.now())
        .returning(table.c.status)
    ).scalar_one_or_none()
    if recorded is not None:
        return str(recorded)
    status = connection.execute(
        select(table.c.status).where(table.c.id == run_id)
    ).scalar_one_or_none()
    return None if status is None else str(status)


def fail_unstarted_report_run(
    connection: Connection,
    *,
    report_run_id: UUID,
    simulation_run_ids: Sequence[UUID],
    error_message: str,
) -> bool:
    """Fail the report run at once while it is pending with no Modal call ID.

    When it was failed, also fail each of ``simulation_run_ids``, the
    simulation runs created for it, that is still pending with no call ID.
    Return whether the report run was failed: one a call has been recorded
    for, or one the coordinator has claimed, is left as it is, and so are
    its simulation runs.
    """
    if not _fail_unstarted(connection, report_runs, report_run_id, error_message):
        return False
    for simulation_run_id in simulation_run_ids:
        _fail_unstarted(connection, simulation_runs, simulation_run_id, error_message)
    return True


def fail_unstarted_simulation_run(
    connection: Connection, *, simulation_run_id: UUID, error_message: str
) -> bool:
    """Fail the simulation run at once while it is pending with no Modal call ID.

    Return whether it was failed: a run a call has been recorded for, or one
    a worker has claimed, is left as it is.
    """
    return _fail_unstarted(
        connection, simulation_runs, simulation_run_id, error_message
    )


def _fail_unstarted(
    connection: Connection, table: Table, run_id: UUID, error_message: str
) -> bool:
    failed = connection.execute(
        update(table)
        .where(*_pending_without_call(table, run_id))
        .values(
            status=_FAILED,
            error_message=error_message,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .returning(table.c.id)
    ).scalar_one_or_none()
    return failed is not None
