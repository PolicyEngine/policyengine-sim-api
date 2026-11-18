"""The simulation service's catalog reads and run writes for economy records.

An economy report compares a baseline and a reform simulation of one
region's data for one year. The service reads the catalog rows a request
names from the Public API's tables (``read_model_id``, ``read_region``,
``read_dataset``), then finds or creates, by definition key, each
simulation, the report and their runs (``keyed_economy_records``). A run
it creates is ``pending`` with no Modal call ID; this module builds and
inserts such runs (``simulation_run_row``, ``insert_simulation_run``).

The service then starts the Modal call that calculates a run it created,
and records the call's ID on that run, or fails it at once when Modal
refuses the call. Both updates apply only while the run is still
``pending`` with no call ID: the coordinator or worker may already have
claimed it, and the stuck-run sweep fails a pending run no call was
recorded for. Failing a report run also fails the simulation runs created
for it, under the same condition. The coordinator and workers write the
rest: ``running``, the results, and ``succeeded`` or ``failed``.

Every function takes an ORM session, so the caller chooses the transactions.
Reads select the mapped classes' attributes, so they return what the database
holds now, whatever the session has loaded. Each update is one conditional
statement that leaves the session's objects alone, and sets only the columns
the service's role may update, with ``updated_at``, whose automatic update
API v1's models make on their own side only. ``references_checked`` turns the
database's refusal of a row naming one that does not exist into
``EconomyRecordReferenceError``.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import (
    ForeignKeyConstraint,
    String,
    cast,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    UK_AREA_REGION_TYPES,
    Dataset,
    Region,
    TaxBenefitModel,
)
from policyengine_simulation_persistence.tables import (
    BudgetWindowRun,
    Report,
    ReportRun,
    ReportRunStatus,
    Simulation,
    SimulationRun,
    SimulationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.orm import Session

# The two classes whose runs a Modal call calculates.
type RunModel = type[SimulationRun | ReportRun]

# PostgreSQL's SQLSTATE for a row naming one that does not exist.
FOREIGN_KEY_VIOLATION: Final = "23503"
# A report's type, as the Public API reads it.
ECONOMY_REPORT_TYPE: Final = "economy"
# Both run tables' statuses use these values.
_PENDING: Final = SimulationStatus.PENDING.value
_FAILED: Final = SimulationStatus.FAILED.value
# Each foreign key of the tables the service writes, by name: its table and
# the first column it constrains.
_KEY_COLUMNS: Final = {
    str(constraint.name): (model.__tablename__, constraint.column_keys[0])
    for model in (Simulation, SimulationRun, Report, ReportRun, BudgetWindowRun)
    for constraint in model.__table_args__
    if isinstance(constraint, ForeignKeyConstraint)
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


def read_model_id(session: Session, *, country: str) -> UUID | None:
    """Return the id of the country's tax-benefit model, or None."""
    return session.execute(
        select(TaxBenefitModel.id).where(
            TaxBenefitModel.name == f"{MODEL_NAME_PREFIX}{country}"
        )
    ).scalar_one_or_none()


def read_region(
    session: Session, *, catalog_version_id: UUID, region_code: str
) -> CatalogRegion | None:
    """Return the catalog version's region with this code, or None."""
    row = session.execute(
        select(Region.id, Region.code, Region.default_dataset_id).where(
            Region.tax_benefit_model_version_id == catalog_version_id,
            Region.code == region_code,
        )
    ).one_or_none()
    if row is None:
        return None
    return CatalogRegion(
        id=row.id, code=row.code, default_dataset_id=row.default_dataset_id
    )


def read_dataset(
    session: Session, *, catalog_version_id: UUID, name: str
) -> CatalogDataset | None:
    """Return the catalog version's dataset with this name, or None."""
    return _dataset(session, catalog_version_id, Dataset.name == name)


def read_dataset_by_id(
    session: Session, *, catalog_version_id: UUID, dataset_id: UUID
) -> CatalogDataset | None:
    """Return the catalog version's dataset with this id, or None."""
    return _dataset(session, catalog_version_id, Dataset.id == dataset_id)


def read_local_area_dataset(
    session: Session, *, catalog_version_id: UUID
) -> CatalogDataset | None:
    """Return the catalog version's local-area dataset, or None when it has none.

    It is the one default dataset every constituency and local-authority
    region of the version uses. The Public API's catalog lists those regions
    only when the .py release routes them to a dataset of their own, and
    gives them that dataset, so a version with no such regions, with only
    one of the two types, or with a dataset for each type has none: its
    national reports calculate both area levels themselves. A report created
    with ``data_source`` runs on this dataset, which may be the national
    file: Stage 12 leaves the areas out of a UK-wide or nation run whenever
    the release routes both types to one dataset
    (``uk_area_breakdowns_omitted``).
    """
    region_type = cast(Region.region_type, String)
    rows = session.execute(
        select(region_type.label("region_type"), Dataset.id, Dataset.name)
        .select_from(Region)
        .distinct()
        .join(
            Dataset,
            (Dataset.id == Region.default_dataset_id)
            & (
                Dataset.tax_benefit_model_version_id
                == Region.tax_benefit_model_version_id
            ),
        )
        .where(
            Region.tax_benefit_model_version_id == catalog_version_id,
            region_type.in_(UK_AREA_REGION_TYPES),
        )
    ).all()
    region_types = {row.region_type for row in rows}
    found = {CatalogDataset(id=row.id, name=row.name) for row in rows}
    if region_types != set(UK_AREA_REGION_TYPES) or len(found) != 1:
        return None
    return found.pop()


def _dataset(
    session: Session, catalog_version_id: UUID, condition: ColumnElement[bool]
) -> CatalogDataset | None:
    row = session.execute(
        select(Dataset.id, Dataset.name).where(
            Dataset.tax_benefit_model_version_id == catalog_version_id, condition
        )
    ).one_or_none()
    return None if row is None else CatalogDataset(id=row.id, name=row.name)


def insert_simulation_run(  # noqa: PLR0913  # the run, what it calculates on, and its marker
    session: Session,
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
    a forced re-run's run, which nothing joins. The statement runs at once,
    so a report run naming the new run finds it.
    """
    session.execute(
        insert(SimulationRun).values(
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
    model: RunModel, run_id: UUID
) -> tuple[ColumnElement[bool], ...]:
    return (
        model.id == run_id,
        model.status == _PENDING,
        model.modal_call_id.is_(None),
    )


def record_report_run_call(
    session: Session, *, report_run_id: UUID, modal_call_id: str
) -> ReportRunStatus | None:
    """Record the report run's Modal call ID while it is pending with none.

    Return the run's status afterwards, or None when there is no such run. A
    run the coordinator has claimed already keeps its claim.
    """
    status = _record_call(session, ReportRun, report_run_id, modal_call_id)
    return None if status is None else ReportRunStatus(status)


def record_simulation_run_call(
    session: Session, *, simulation_run_id: UUID, modal_call_id: str
) -> SimulationStatus | None:
    """Record the simulation run's Modal call ID while it is pending with none.

    Return the run's status afterwards, or None when there is no such run. A
    run the worker has claimed already keeps its claim.
    """
    status = _record_call(session, SimulationRun, simulation_run_id, modal_call_id)
    return None if status is None else SimulationStatus(status)


def _record_call(
    session: Session, model: RunModel, run_id: UUID, modal_call_id: str
) -> str | None:
    recorded = session.execute(
        update(model)
        .where(*_pending_without_call(model, run_id))
        .values(modal_call_id=modal_call_id, updated_at=func.now())
        .returning(model.status)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if recorded is not None:
        return str(recorded)
    status = session.execute(
        select(model.status).where(model.id == run_id)
    ).scalar_one_or_none()
    return None if status is None else str(status)


def fail_unstarted_report_run(
    session: Session,
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
    if not _fail_unstarted(session, ReportRun, report_run_id, error_message):
        return False
    for simulation_run_id in simulation_run_ids:
        _fail_unstarted(session, SimulationRun, simulation_run_id, error_message)
    return True


def fail_unstarted_simulation_run(
    session: Session, *, simulation_run_id: UUID, error_message: str
) -> bool:
    """Fail the simulation run at once while it is pending with no Modal call ID.

    Return whether it was failed: a run a call has been recorded for, or one
    a worker has claimed, is left as it is.
    """
    return _fail_unstarted(session, SimulationRun, simulation_run_id, error_message)


def _fail_unstarted(
    session: Session, model: RunModel, run_id: UUID, error_message: str
) -> bool:
    failed = session.execute(
        update(model)
        .where(*_pending_without_call(model, run_id))
        .values(
            status=_FAILED,
            error_message=error_message,
            completed_at=func.now(),
            updated_at=func.now(),
        )
        .returning(model.id)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    return failed is not None
