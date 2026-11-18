"""Find a shared simulation, report or run, or insert it: one lookup each.

Identical requests share simulations, reports and runs (C1 · 24). Each
lookup here is one ``INSERT … ON CONFLICT DO NOTHING`` against API v1's
unique index for the row's definition or version set, followed, when the
insert did nothing, by a read of the row that won:

- a simulation or a report by ``(key_version, definition_key)``;
- a simulation run by simulation, year, catalog version, country package
  version and .py version, among runs that haven't failed and weren't forced;
- a report run by report, country package version and .py version, the
  same way, outside budget windows, whose year runs no index covers;
- a budget-window run by report, catalog version and .py version, among
  window runs that haven't failed and aren't recalculations (answer 8);
- a simulation run's cliff calculation (``CliffImpact``) by its run, among
  calculations that haven't failed (answer 4b).

Under READ COMMITTED an insert that conflicts with another transaction's
uncommitted row waits until that transaction ends. Once it commits, the
insert does nothing and the read, which takes a new snapshot, sees the row,
so concurrent identical requests end on one row. A run read as missing
after its insert did nothing has failed in between, which makes it a run
nobody joins (answer 2), so the insert is tried again.

A forced re-run's runs are inserted directly, marked ``forced``: no index
covers them, so they never conflict, never join and are never joined
(answer 3). Callers choose the transactions by the ORM session they pass, and
insert rows in one order (definitions by key, runs by simulation ID), so two
transactions never wait for each other in opposite orders. Each lookup is one
statement on a mapped class, run at once, so it leaves the session nothing to
flush and no object loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Index, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from policyengine_simulation_persistence.tables import (
    BudgetWindowRun,
    CliffImpact,
    Report,
    ReportRun,
    ReportRunStatus,
    Simulation,
    SimulationRun,
    SimulationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from sqlalchemy import ColumnElement, Row
    from sqlalchemy.orm import InstrumentedAttribute, Session

# The classes a lookup inserts into.
type SharedRowModel = type[
    Simulation | Report | SimulationRun | ReportRun | BudgetWindowRun | CliffImpact
]

# How many times a lookup inserts before giving up on a row that keeps
# failing between its insert and its read.
INSERT_ATTEMPTS: Final = 3
_DEFINITION_COLUMNS: Final = ("key_version", "definition_key")


class SharedRowNotSettledError(RuntimeError):
    """A run kept failing between a lookup's insert and its read.

    Nothing was inserted; the caller's transaction may go on or end.
    """


@dataclass(frozen=True)
class FoundRow:
    """A row a lookup found, or inserted itself (``inserted``)."""

    id: UUID
    inserted: bool


@dataclass(frozen=True)
class FoundSimulationRun:
    """A simulation run a lookup found or inserted, with its status."""

    id: UUID
    inserted: bool
    status: SimulationStatus


@dataclass(frozen=True)
class FoundReportRun:
    """A report run a lookup found or inserted, with its status and pair."""

    id: UUID
    inserted: bool
    status: ReportRunStatus
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID | None


@dataclass(frozen=True)
class FoundWindowRun:
    """A budget-window run a lookup found or inserted, with its status."""

    id: UUID
    inserted: bool
    status: ReportRunStatus


@dataclass(frozen=True)
class FoundCliffCalculation:
    """A simulation run's cliff calculation a lookup found or inserted.

    ``status`` is the calculation's: pending or running while it is being
    calculated, succeeded once it holds the run's cliff gap and share.
    """

    id: UUID
    simulation_run_id: UUID
    inserted: bool
    status: SimulationStatus


@dataclass(frozen=True)
class ProposedCliffs:
    """The IDs a request asking for cliffs proposes for its sides' calculations.

    Each is used only for a calculation the request inserts; one run on
    both sides gets one calculation, with the baseline's ID.
    """

    baseline_id: UUID
    reform_id: UUID


@dataclass(frozen=True)
class FoundCliffs:
    """Each side's simulation run's cliff calculation, found or inserted."""

    baseline: FoundCliffCalculation
    reform: FoundCliffCalculation


@dataclass(frozen=True)
class _UniqueIndex:
    """A unique index a lookup inserts against: its columns and condition."""

    model: SharedRowModel
    columns: tuple[str, ...]
    # The partial index's predicate, as the model declares it; None for a
    # unique constraint over the whole table.
    where: ColumnElement[bool] | None
    # The same predicate in SQLAlchemy terms, for the read.
    read_conditions: tuple[ColumnElement[bool], ...]


def _partial_index(
    model: SharedRowModel, name: str, read_conditions: tuple[ColumnElement[bool], ...]
) -> _UniqueIndex:
    """Return the model's partial unique index ``name``, with its own predicate."""
    (index,) = (
        argument
        for argument in model.__table_args__
        if isinstance(argument, Index) and argument.name == name
    )
    return _UniqueIndex(
        model=model,
        columns=tuple(column.name for column in index.columns),
        where=index.dialect_options["postgresql"]["where"],
        read_conditions=read_conditions,
    )


_SIMULATION_DEFINITION: Final = _UniqueIndex(
    model=Simulation, columns=_DEFINITION_COLUMNS, where=None, read_conditions=()
)
_REPORT_DEFINITION: Final = _UniqueIndex(
    model=Report, columns=_DEFINITION_COLUMNS, where=None, read_conditions=()
)
_JOINABLE_SIMULATION_RUN: Final = _partial_index(
    SimulationRun,
    "uq_simulation_runs_unfailed_unforced_versions",
    (
        SimulationRun.status != SimulationStatus.FAILED,
        SimulationRun.forced.is_(False),
    ),
)
_JOINABLE_REPORT_RUN: Final = _partial_index(
    ReportRun,
    "uq_report_runs_unfailed_unforced_versions",
    (
        ReportRun.budget_window_run_id.is_(None),
        ReportRun.status != ReportRunStatus.FAILED,
        ReportRun.forced.is_(False),
    ),
)


def find_or_insert_simulation(session: Session, values: Mapping[str, Any]) -> FoundRow:
    """Return the simulation with ``values``' key, inserting ``values`` if none.

    ``values`` is the whole row, its ``key_version`` and ``definition_key``
    included.
    """
    row, inserted = _find_or_insert(session, _SIMULATION_DEFINITION, values, ())
    return FoundRow(id=row.id, inserted=inserted)


def find_or_insert_report(session: Session, values: Mapping[str, Any]) -> FoundRow:
    """Return the report with ``values``' key, inserting ``values`` if none.

    ``values`` is the whole row, its three key columns included.
    """
    row, inserted = _find_or_insert(session, _REPORT_DEFINITION, values, ())
    return FoundRow(id=row.id, inserted=inserted)


def find_or_insert_simulation_run(
    session: Session, values: Mapping[str, Any]
) -> FoundSimulationRun:
    """Return the simulation's joinable run on ``values``' versions, or insert it.

    A joinable run hasn't failed and wasn't forced. ``values`` is the whole
    row; a forced run is never looked up (ValueError): insert it directly.
    """
    _refuse_forced(values)
    row, inserted = _find_or_insert(
        session, _JOINABLE_SIMULATION_RUN, values, (SimulationRun.status,)
    )
    return FoundSimulationRun(
        id=row.id, inserted=inserted, status=SimulationStatus(row.status)
    )


def find_or_insert_simulation_runs(
    session: Session, rows: Mapping[UUID, Mapping[str, Any]]
) -> dict[UUID, FoundSimulationRun]:
    """Return each simulation's joinable run, by simulation ID, or insert ``rows``.

    ``rows`` maps each simulation's ID to its run as it would be inserted.
    The lookups go in simulation ID order, so two transactions never wait
    for each other in opposite orders.
    """
    return {
        simulation_id: find_or_insert_simulation_run(session, rows[simulation_id])
        for simulation_id in sorted(rows)
    }


_JOINABLE_WINDOW_RUN: Final = _partial_index(
    BudgetWindowRun,
    "uq_budget_window_runs_unfailed_unforced_versions",
    (
        BudgetWindowRun.status != ReportRunStatus.FAILED,
        BudgetWindowRun.recalculate.is_(False),
    ),
)


def find_or_insert_window_run(
    session: Session, values: Mapping[str, Any]
) -> FoundWindowRun:
    """Return the report's joinable window run on ``values``' versions, or insert it.

    A joinable window run hasn't failed and isn't a recalculation, a forced
    re-run's, which is never looked up (ValueError). ``values`` is the
    whole row, its ``policyengine_version`` included.
    """
    if values.get("recalculate"):
        msg = "a recalculating window run is inserted directly: nothing joins it"
        raise ValueError(msg)
    row, inserted = _find_or_insert(
        session, _JOINABLE_WINDOW_RUN, values, (BudgetWindowRun.status,)
    )
    return FoundWindowRun(
        id=row.id, inserted=inserted, status=ReportRunStatus(row.status)
    )


def find_joinable_window_run(
    session: Session,
    *,
    report_id: UUID,
    tax_benefit_model_version_id: UUID,
    policyengine_version: str,
) -> FoundWindowRun | None:
    """Return the report's joinable window run on these versions, or None.

    The one ``find_or_insert_window_run`` would join. A window run created
    before C1 · 27 records no .py version, so it is never joined.
    """
    row = session.execute(
        select(BudgetWindowRun.id, BudgetWindowRun.status).where(
            BudgetWindowRun.report_id == report_id,
            BudgetWindowRun.tax_benefit_model_version_id
            == tax_benefit_model_version_id,
            BudgetWindowRun.policyengine_version == policyengine_version,
            *_JOINABLE_WINDOW_RUN.read_conditions,
        )
    ).one_or_none()
    if row is None:
        return None
    return FoundWindowRun(id=row.id, inserted=False, status=ReportRunStatus(row.status))


def find_or_insert_report_run(
    session: Session, values: Mapping[str, Any]
) -> FoundReportRun:
    """Return the report's joinable run on ``values``' versions, or insert it.

    A joinable run hasn't failed and wasn't forced. ``values`` is the whole
    row. A forced run, or a budget window's year run, which no index covers,
    is never looked up (ValueError).
    """
    _refuse_forced(values)
    if values.get("budget_window_run_id") is not None:
        msg = "a budget window's year runs are never looked up by versions"
        raise ValueError(msg)
    row, inserted = _find_or_insert(
        session,
        _JOINABLE_REPORT_RUN,
        values,
        (
            ReportRun.status,
            ReportRun.baseline_simulation_run_id,
            ReportRun.reform_simulation_run_id,
        ),
    )
    return FoundReportRun(
        id=row.id,
        inserted=inserted,
        status=ReportRunStatus(row.status),
        baseline_simulation_run_id=row.baseline_simulation_run_id,
        reform_simulation_run_id=row.reform_simulation_run_id,
    )


def find_joinable_report_run(
    session: Session,
    *,
    report_id: UUID,
    country_package_version: str,
    policyengine_version: str,
) -> FoundReportRun | None:
    """Return the report's joinable run on these versions, or None.

    The run hasn't failed, wasn't forced, and is no budget window's year
    run: the one ``find_or_insert_report_run`` would join.
    """
    row = session.execute(
        select(
            ReportRun.id,
            ReportRun.status,
            ReportRun.baseline_simulation_run_id,
            ReportRun.reform_simulation_run_id,
        ).where(
            ReportRun.report_id == report_id,
            ReportRun.country_package_version == country_package_version,
            ReportRun.policyengine_version == policyengine_version,
            *_JOINABLE_REPORT_RUN.read_conditions,
        )
    ).one_or_none()
    if row is None:
        return None
    return FoundReportRun(
        id=row.id,
        inserted=False,
        status=ReportRunStatus(row.status),
        baseline_simulation_run_id=row.baseline_simulation_run_id,
        reform_simulation_run_id=row.reform_simulation_run_id,
    )


_UNFAILED_CLIFF_CALCULATION: Final = _partial_index(
    CliffImpact,
    "uq_cliff_impacts_unfailed",
    (CliffImpact.status != SimulationStatus.FAILED,),
)


def find_or_insert_cliff_calculations(
    session: Session, proposed: Mapping[UUID, UUID]
) -> dict[UUID, FoundCliffCalculation]:
    """Return each simulation run's cliff calculation, inserting it when it has none.

    ``proposed`` maps each simulation run's ID to the ID its calculation
    gets if this call inserts it, pending with no Modal call. A run's
    calculation that hasn't failed, pending, running or succeeded, is
    found instead: a run has one cliff result, and a failed calculation
    never counts (answer 2's rule, applied to cliffs). The lookups go in
    simulation run ID order, so two transactions never wait for each other
    in opposite orders. Return the calculations by simulation run ID.
    """
    found: dict[UUID, FoundCliffCalculation] = {}
    for simulation_run_id in sorted(proposed):
        row, inserted = _find_or_insert(
            session,
            _UNFAILED_CLIFF_CALCULATION,
            {
                "id": proposed[simulation_run_id],
                "simulation_run_id": simulation_run_id,
                "status": SimulationStatus.PENDING,
            },
            (CliffImpact.status,),
        )
        found[simulation_run_id] = FoundCliffCalculation(
            id=row.id,
            simulation_run_id=simulation_run_id,
            inserted=inserted,
            status=SimulationStatus(row.status),
        )
    return found


def find_or_insert_side_cliffs(
    session: Session,
    proposed: ProposedCliffs,
    *,
    baseline_simulation_run_id: UUID,
    reform_simulation_run_id: UUID,
) -> FoundCliffs:
    """Return each side's run's cliff calculation, inserting those it lacks.

    Called in the transaction that settled the report run naming the two
    runs (``keyed_economy_records.find_or_insert_report_run_cliffs``), so
    the calculations are written with it, or not at all.
    """
    proposed_by_run = {baseline_simulation_run_id: proposed.baseline_id}
    proposed_by_run.setdefault(reform_simulation_run_id, proposed.reform_id)
    found = find_or_insert_cliff_calculations(session, proposed_by_run)
    return FoundCliffs(
        baseline=found[baseline_simulation_run_id],
        reform=found[reform_simulation_run_id],
    )


def _refuse_forced(values: Mapping[str, Any]) -> None:
    if values.get("forced"):
        msg = "a forced run is inserted directly: it never joins or is joined"
        raise ValueError(msg)


def _find_or_insert(
    session: Session,
    index: _UniqueIndex,
    values: Mapping[str, Any],
    returned: Sequence[InstrumentedAttribute[Any]],
) -> tuple[Row[Any], bool]:
    """Insert ``values`` unless ``index`` holds a row like it; return the row.

    Return the row (its ID, then ``returned``) and whether this call
    inserted it. Raise ValueError when ``values`` lacks a column of the
    index, and SharedRowNotSettledError when the row that won keeps failing
    before it is read.
    """
    missing = [name for name in index.columns if values.get(name) is None]
    if missing:
        msg = f"the row names no {', '.join(missing)}, which find it"
        raise ValueError(msg)
    model = index.model
    columns = (model.id, *returned)
    statement = (
        postgresql_insert(model)
        .values(dict(values))
        .on_conflict_do_nothing(index_elements=index.columns, index_where=index.where)
        .returning(*columns)
    )
    winner = select(*columns).where(
        *(getattr(model, name) == values[name] for name in index.columns),
        *index.read_conditions,
    )
    for _attempt in range(INSERT_ATTEMPTS):
        inserted = session.execute(statement).one_or_none()
        if inserted is not None:
            return inserted, True
        found = session.execute(winner).one_or_none()
        if found is not None:
            return found, False
    msg = (
        f"the {model.__tablename__} row that won kept failing before it was read, "
        f"{INSERT_ATTEMPTS} times"
    )
    raise SharedRowNotSettledError(msg)
