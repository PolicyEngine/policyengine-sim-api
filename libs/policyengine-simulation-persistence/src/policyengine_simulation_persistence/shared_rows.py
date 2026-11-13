"""Find a shared simulation, report or run, or insert it: one lookup each.

Identical requests share simulations, reports and runs (C1 · 24). Each
lookup here is one ``INSERT … ON CONFLICT DO NOTHING`` against the Public
API's unique index for the row's definition or version set, followed, when
the insert did nothing, by a read of the row that won:

- a simulation or a report by ``(key_version, definition_key)``;
- a simulation run by simulation, year, catalog version, country package
  version and .py version, among runs that haven't failed and weren't forced;
- a report run by report, country package version and .py version, the
  same way, outside budget windows, whose year runs no index covers.

Under READ COMMITTED an insert that conflicts with another transaction's
uncommitted row waits until that transaction ends. Once it commits, the
insert does nothing and the read, which takes a new snapshot, sees the row,
so concurrent identical requests end on one row. A run read as missing
after its insert did nothing has failed in between, which makes it a run
nobody joins (answer 2), so the insert is tried again.

A forced re-run's runs are inserted directly, marked ``forced``: no index
covers them, so they never conflict, never join and are never joined
(answer 3). Callers choose the transactions, and insert rows in one order
(definitions by key, runs by simulation ID), so two transactions never wait
for each other in opposite orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    SimulationStatus,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, Row, Table

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
class _UniqueIndex:
    """A unique index a lookup inserts against: its columns and condition."""

    table: Table
    columns: tuple[str, ...]
    # The partial index's predicate, as the model declares it; None for a
    # unique constraint over the whole table.
    where: ColumnElement[bool] | None
    # The same predicate in SQLAlchemy terms, for the read.
    read_conditions: tuple[ColumnElement[bool], ...]


def _partial_index(
    table: Table, name: str, read_conditions: tuple[ColumnElement[bool], ...]
) -> _UniqueIndex:
    """Return the model's partial unique index ``name``, with its own predicate."""
    (index,) = (index for index in table.indexes if index.name == name)
    return _UniqueIndex(
        table=table,
        columns=tuple(column.name for column in index.columns),
        where=index.dialect_options["postgresql"]["where"],
        read_conditions=read_conditions,
    )


_SIMULATION_DEFINITION: Final = _UniqueIndex(
    table=simulations, columns=_DEFINITION_COLUMNS, where=None, read_conditions=()
)
_REPORT_DEFINITION: Final = _UniqueIndex(
    table=reports, columns=_DEFINITION_COLUMNS, where=None, read_conditions=()
)
_JOINABLE_SIMULATION_RUN: Final = _partial_index(
    simulation_runs,
    "uq_simulation_runs_unfailed_unforced_versions",
    (
        simulation_runs.c.status != SimulationStatus.FAILED,
        simulation_runs.c.forced.is_(False),
    ),
)
_JOINABLE_REPORT_RUN: Final = _partial_index(
    report_runs,
    "uq_report_runs_unfailed_unforced_versions",
    (
        report_runs.c.budget_window_run_id.is_(None),
        report_runs.c.status != ReportRunStatus.FAILED,
        report_runs.c.forced.is_(False),
    ),
)


def find_or_insert_simulation(
    connection: Connection, values: Mapping[str, Any]
) -> FoundRow:
    """Return the simulation with ``values``' key, inserting ``values`` if none.

    ``values`` is the whole row, its ``key_version`` and ``definition_key``
    included.
    """
    row, inserted = _find_or_insert(connection, _SIMULATION_DEFINITION, values, ())
    return FoundRow(id=row.id, inserted=inserted)


def find_or_insert_report(
    connection: Connection, values: Mapping[str, Any]
) -> FoundRow:
    """Return the report with ``values``' key, inserting ``values`` if none.

    ``values`` is the whole row, its three key columns included.
    """
    row, inserted = _find_or_insert(connection, _REPORT_DEFINITION, values, ())
    return FoundRow(id=row.id, inserted=inserted)


def find_or_insert_simulation_run(
    connection: Connection, values: Mapping[str, Any]
) -> FoundSimulationRun:
    """Return the simulation's joinable run on ``values``' versions, or insert it.

    A joinable run hasn't failed and wasn't forced. ``values`` is the whole
    row; a forced run is never looked up (ValueError): insert it directly.
    """
    _refuse_forced(values)
    row, inserted = _find_or_insert(
        connection, _JOINABLE_SIMULATION_RUN, values, (simulation_runs.c.status,)
    )
    return FoundSimulationRun(
        id=row.id, inserted=inserted, status=SimulationStatus(row.status)
    )


def find_or_insert_simulation_runs(
    connection: Connection, rows: Mapping[UUID, Mapping[str, Any]]
) -> dict[UUID, FoundSimulationRun]:
    """Return each simulation's joinable run, by simulation ID, or insert ``rows``.

    ``rows`` maps each simulation's ID to its run as it would be inserted.
    The lookups go in simulation ID order, so two transactions never wait
    for each other in opposite orders.
    """
    return {
        simulation_id: find_or_insert_simulation_run(connection, rows[simulation_id])
        for simulation_id in sorted(rows)
    }


def find_or_insert_report_run(
    connection: Connection, values: Mapping[str, Any]
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
        connection,
        _JOINABLE_REPORT_RUN,
        values,
        (
            report_runs.c.status,
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
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
    connection: Connection,
    *,
    report_id: UUID,
    country_package_version: str,
    policyengine_version: str,
) -> FoundReportRun | None:
    """Return the report's joinable run on these versions, or None.

    The run hasn't failed, wasn't forced, and is no budget window's year
    run: the one ``find_or_insert_report_run`` would join.
    """
    row = connection.execute(
        select(
            report_runs.c.id,
            report_runs.c.status,
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        ).where(
            report_runs.c.report_id == report_id,
            report_runs.c.country_package_version == country_package_version,
            report_runs.c.policyengine_version == policyengine_version,
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


def _refuse_forced(values: Mapping[str, Any]) -> None:
    if values.get("forced"):
        msg = "a forced run is inserted directly: it never joins or is joined"
        raise ValueError(msg)


def _find_or_insert(
    connection: Connection,
    index: _UniqueIndex,
    values: Mapping[str, Any],
    returned: Sequence[ColumnElement[Any]],
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
    table = index.table
    columns = (table.c.id, *returned)
    statement = (
        postgresql_insert(table)
        .values(dict(values))
        .on_conflict_do_nothing(index_elements=index.columns, index_where=index.where)
        .returning(*columns)
    )
    winner = select(*columns).where(
        *(table.c[name] == values[name] for name in index.columns),
        *index.read_conditions,
    )
    for _attempt in range(INSERT_ATTEMPTS):
        inserted = connection.execute(statement).one_or_none()
        if inserted is not None:
            return inserted, True
        found = connection.execute(winner).one_or_none()
        if found is not None:
            return found, False
    msg = (
        f"the {table.name} row that won kept failing before it was read, "
        f"{INSERT_ATTEMPTS} times"
    )
    raise SharedRowNotSettledError(msg)
