"""The restart check's reads and writes, for runs whose Modal call never started.

The simulation service creates a run or household job ``pending`` with no
Modal call ID, starts its call, then records the call's ID. When the service
stops between the insert and the start, nothing ever records a call, and the
stuck-run sweep would fail the run. The restart check, a scheduled Modal
function, starts such a run's call once more:

1. ``list_unstarted_runs`` lists the rows still pending with no call that were
   created within a window and that nothing has changed since their insert
   (``updated_at`` still equals ``created_at``): household jobs, report runs,
   and standalone simulation runs, which no report run names. A report run's
   own simulation runs are its coordinator's to start. Budget-window years
   are left out, and household simulations too: the check can't rebuild
   their calls yet.
   ``list_idle_budget_windows`` lists, apart, the budget windows that
   stopped: no year is under way and years are still waiting, untouched,
   since a time in the window. ``read_earliest_waiting_year`` names the year
   a restart starts the window from, and marks.
2. ``mark_restart`` sets the row's ``updated_at`` before the check starts its
   call, so the row is never listed again and is restarted at most once. It
   uses the statement's clock rather than the transaction's, so the mark
   differs from a creation time in the same transaction too.
3. ``read_unstarted_report_run`` and ``read_unstarted_simulation_run`` read
   back what the service built the call from: each side's simulation and
   values, read against the run's own catalog version, the region, dataset
   and year the run records, and the report's options.

The check then records its call's ID, or fails the row, with the service's
own conditional writes (``record_report_run_call`` and the rest). Every
function here takes a connection, so the caller chooses the transactions,
and every update sets only columns both the service's and the Modal worker's
roles may update.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import and_, exists, func, or_, select, update

from policyengine_simulation_persistence.economy_run_records import (
    RunVersions,
    SimulationChoice,
)
from policyengine_simulation_persistence.economy_run_store import (
    read_simulation_reform,
)
from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    datasets,
    regions,
    tax_benefit_model_versions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    SimulationStatus,
    SimulationType,
    budget_window_runs,
    household_jobs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, Row, Select, Table

    from policyengine_simulation_persistence.policy_reform import PolicyReform

# Every restartable table's status uses this value for a row not yet claimed.
_PENDING: Final = SimulationStatus.PENDING.value


class RestartKind(StrEnum):
    """What kind of row the restart check starts again."""

    HOUSEHOLD_JOB = "household_job"
    REPORT_RUN = "report_run"
    SIMULATION_RUN = "simulation_run"
    # A budget window none of whose years is under way: a restart starts its
    # earliest waiting years, as the service starts a new window's.
    BUDGET_WINDOW = "budget_window"


# The table of each kind restarted one row at a time.
RESTART_TABLES: Final[dict[RestartKind, Table]] = {
    RestartKind.HOUSEHOLD_JOB: household_jobs,
    RestartKind.REPORT_RUN: report_runs,
    RestartKind.SIMULATION_RUN: simulation_runs,
}


@dataclass(frozen=True)
class UnstartedRun:
    """A row still pending with no Modal call, and since when it has waited.

    ``created_at`` is a row's creation, and a budget window's last change:
    the latest change of the window or any of its years.
    """

    kind: RestartKind
    run_id: UUID
    created_at: datetime


class StoredRunUnreadableError(LookupError):
    """A run's stored rows no longer describe one calculation to start."""


@dataclass(frozen=True)
class WaitingYear:
    """A budget window's year still pending with no call, never changed since."""

    report_run_id: UUID
    year: int


@dataclass(frozen=True)
class RunCall:
    """A row's status and the Modal call it records, if any."""

    status: str
    modal_call_id: str | None


@dataclass(frozen=True)
class EconomyScope:
    """What every simulation of an economy run calculates on, as stored.

    ``catalog_version_id`` is the catalog version the run was created on, and
    its region and dataset are named by code and name, as the worker reads
    them.
    """

    catalog_version_id: UUID
    region_code: str
    dataset_name: str
    year: int


@dataclass(frozen=True)
class StoredEconomySide:
    """One simulation run of an economy run, with its simulation's values.

    ``reform`` holds the simulation's policy and dynamic values together, by
    parameter name and period, read against the run's catalog version.
    """

    simulation_run_id: UUID
    choice: SimulationChoice
    reform: PolicyReform


@dataclass(frozen=True)
class StoredReportRun:
    """A report run as its coordinator's call was built from it.

    ``options`` are the report's (``reports.inputs``), and ``versions`` those
    the report run records, which choose the worker that calculates it.
    """

    report_run_id: UUID
    country: str
    versions: RunVersions
    options: dict[str, Any]
    scope: EconomyScope
    baseline: StoredEconomySide
    reform: StoredEconomySide
    created_at: datetime


@dataclass(frozen=True)
class StoredSimulationRun:
    """A standalone simulation run as its simulation function's call was built."""

    country: str
    versions: RunVersions
    scope: EconomyScope
    run: StoredEconomySide
    created_at: datetime


def _unstarted(table: Table) -> tuple[ColumnElement[bool], ...]:
    """Return the conditions that a row is pending, with no call, never changed."""
    return (
        table.c.status == _PENDING,
        table.c.modal_call_id.is_(None),
        table.c.updated_at == table.c.created_at,
    )


def _economy_simulation_run(
    simulation_run_id: ColumnElement[Any],
) -> ColumnElement[bool]:
    """Return the condition that the simulation run is an economy simulation's."""
    return exists().where(
        simulations.c.id == simulation_runs.c.simulation_id,
        simulation_runs.c.id == simulation_run_id,
        simulations.c.simulation_type == SimulationType.ECONOMY,
    )


def _restart_candidates(kind: RestartKind) -> Select[Any]:
    """Return the query for one kind's unstarted rows, before the window."""
    table = RESTART_TABLES[kind]
    statement = select(table.c.id, table.c.created_at).where(*_unstarted(table))
    if kind is RestartKind.REPORT_RUN:
        return statement.where(
            report_runs.c.budget_window_run_id.is_(None),
            _economy_simulation_run(report_runs.c.baseline_simulation_run_id),
        )
    if kind is RestartKind.SIMULATION_RUN:
        naming_report_run = report_runs.alias("naming_report_run")
        return statement.where(
            exists().where(
                simulations.c.id == simulation_runs.c.simulation_id,
                simulations.c.simulation_type == SimulationType.ECONOMY,
            ),
            ~exists().where(
                or_(
                    naming_report_run.c.baseline_simulation_run_id
                    == simulation_runs.c.id,
                    naming_report_run.c.reform_simulation_run_id
                    == simulation_runs.c.id,
                )
            ),
        )
    return statement


def list_unstarted_runs(
    connection: Connection,
    *,
    created_after: datetime,
    created_before: datetime,
    limit: int,
) -> tuple[UnstartedRun, ...]:
    """Return the oldest restartable rows created in the window, at most ``limit``.

    A row is restartable while it is pending with no Modal call recorded and
    nothing has changed it since its insert. Report runs and standalone
    simulation runs are listed only for economy simulations, and report runs
    only outside a budget window.
    """
    listed: list[UnstartedRun] = []
    for kind, table in RESTART_TABLES.items():
        statement = (
            _restart_candidates(kind)
            .where(
                table.c.created_at > created_after,
                table.c.created_at <= created_before,
            )
            .order_by(table.c.created_at, table.c.id)
            .limit(limit)
        )
        listed.extend(
            UnstartedRun(kind=kind, run_id=row.id, created_at=row.created_at)
            for row in connection.execute(statement)
        )
    listed.sort(key=lambda run: (run.created_at, run.run_id))
    return tuple(listed[:limit])


_ACTIVE_WINDOW_STATUSES: Final = (
    ReportRunStatus.PENDING.value,
    ReportRunStatus.RUNNING.value,
)


def _year_of_window() -> ColumnElement[bool]:
    return report_runs.c.budget_window_run_id == budget_window_runs.c.id


def _year_waiting() -> ColumnElement[bool]:
    return and_(report_runs.c.status == _PENDING, report_runs.c.modal_call_id.is_(None))


def _year_under_way() -> ColumnElement[bool]:
    return or_(
        report_runs.c.status == ReportRunStatus.RUNNING.value,
        and_(
            report_runs.c.status == _PENDING,
            report_runs.c.modal_call_id.is_not(None),
        ),
    )


def list_idle_budget_windows(
    connection: Connection,
    *,
    idle_after: datetime,
    idle_before: datetime,
    limit: int,
) -> tuple[UnstartedRun, ...]:
    """Return the stopped budget windows last changed in the window, oldest first.

    A window has stopped when it is pending or running, none of its years is
    under way (running, or pending with a call), and years are still waiting
    (pending with no call) that nothing has changed since their insert: the
    service stopped before starting its first years, or a year's coordinator
    before starting the next. Its last change is the latest ``updated_at`` of
    the window and its years. A window whose waiting year was marked by a
    restart is not listed again until that year has started.
    """
    last_year_change = (
        select(func.max(report_runs.c.updated_at))
        .where(_year_of_window())
        .scalar_subquery()
    )
    last_change = func.greatest(
        budget_window_runs.c.updated_at,
        func.coalesce(last_year_change, budget_window_runs.c.updated_at),
    )
    statement = (
        select(budget_window_runs.c.id, last_change.label("last_change"))
        .where(
            budget_window_runs.c.status.in_(_ACTIVE_WINDOW_STATUSES),
            ~exists().where(_year_of_window(), _year_under_way()),
            exists().where(_year_of_window(), _year_waiting()),
            ~exists().where(
                _year_of_window(),
                _year_waiting(),
                report_runs.c.updated_at != report_runs.c.created_at,
            ),
            last_change > idle_after,
            last_change <= idle_before,
        )
        .order_by(last_change, budget_window_runs.c.id)
        .limit(limit)
    )
    return tuple(
        UnstartedRun(
            kind=RestartKind.BUDGET_WINDOW, run_id=row.id, created_at=row.last_change
        )
        for row in connection.execute(statement)
    )


def read_earliest_waiting_year(
    connection: Connection, window_run_id: UUID
) -> WaitingYear | None:
    """Return the window's earliest year still waiting, never changed, or None."""
    row = connection.execute(
        select(report_runs.c.id, report_runs.c.year)
        .where(
            report_runs.c.budget_window_run_id == window_run_id,
            *_unstarted(report_runs),
        )
        .order_by(report_runs.c.year)
        .limit(1)
    ).one_or_none()
    return None if row is None else WaitingYear(report_run_id=row.id, year=row.year)


def mark_restart(connection: Connection, run: UnstartedRun) -> bool:
    """Mark the row as restarted while it is still unstarted; return whether it was.

    A row given a call, claimed, finished or already marked since it was
    listed is left as it is, so two checks at once restart it only once.
    """
    table = RESTART_TABLES[run.kind]
    marked = connection.execute(
        update(table)
        .where(table.c.id == run.run_id, *_unstarted(table))
        .values(updated_at=func.clock_timestamp())
        .returning(table.c.id)
    ).scalar_one_or_none()
    return marked is not None


def read_run_call(connection: Connection, run: UnstartedRun) -> RunCall | None:
    """Return the row's status and recorded call, or None when it is gone."""
    table = RESTART_TABLES[run.kind]
    row = connection.execute(
        select(table.c.status, table.c.modal_call_id).where(table.c.id == run.run_id)
    ).one_or_none()
    if row is None:
        return None
    return RunCall(status=str(row.status), modal_call_id=row.modal_call_id)


def read_report_run_simulation_run_ids(
    connection: Connection, report_run_id: UUID
) -> tuple[UUID, ...]:
    """Return the simulation runs the report run names, baseline first.

    Return no IDs when there is no such run. A report run the check can't
    rebuild is failed with them, as its submission would fail it.
    """
    row = connection.execute(
        select(
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        ).where(report_runs.c.id == report_run_id)
    ).one_or_none()
    if row is None:
        return ()
    return tuple(
        run_id
        for run_id in (row.baseline_simulation_run_id, row.reform_simulation_run_id)
        if run_id is not None
    )


def _side_rows(connection: Connection, simulation_run_id: UUID) -> Row[Any] | None:
    """Return a simulation run's scope and its simulation's policy and dynamic."""
    return connection.execute(
        select(
            simulation_runs.c.id,
            simulation_runs.c.created_at,
            simulation_runs.c.year,
            simulation_runs.c.tax_benefit_model_version_id,
            simulation_runs.c.country_package_version,
            simulation_runs.c.policyengine_version,
            simulations.c.policy_id,
            simulations.c.dynamic_id,
            regions.c.code.label("region_code"),
            datasets.c.name.label("dataset_name"),
            tax_benefit_models.c.name.label("model_name"),
        )
        .join(simulations, simulations.c.id == simulation_runs.c.simulation_id)
        .join(
            tax_benefit_model_versions,
            tax_benefit_model_versions.c.id
            == simulation_runs.c.tax_benefit_model_version_id,
        )
        .join(
            tax_benefit_models,
            tax_benefit_models.c.id == tax_benefit_model_versions.c.model_id,
        )
        .outerjoin(regions, regions.c.id == simulation_runs.c.region_id)
        .outerjoin(datasets, datasets.c.id == simulation_runs.c.dataset_id)
        .where(simulation_runs.c.id == simulation_run_id)
    ).one_or_none()


def _scope(side: Row[Any]) -> EconomyScope:
    """Return what the side calculates on; raise when its region or dataset is gone."""
    if side.region_code is None:
        msg = f"simulation run {side.id} names no region"
        raise StoredRunUnreadableError(msg)
    if side.dataset_name is None:
        msg = f"simulation run {side.id} names no dataset"
        raise StoredRunUnreadableError(msg)
    return EconomyScope(
        catalog_version_id=side.tax_benefit_model_version_id,
        region_code=side.region_code,
        dataset_name=side.dataset_name,
        year=side.year,
    )


def _side(
    connection: Connection, country: str, scope: EconomyScope, side: Row[Any]
) -> StoredEconomySide:
    """Read the side's policy and dynamic values against the scope's catalog version.

    Raise the policy reader's errors, or OverlappingReformError.
    """
    choice = SimulationChoice(policy_id=side.policy_id, dynamic_id=side.dynamic_id)
    return StoredEconomySide(
        simulation_run_id=side.id,
        choice=choice,
        reform=read_simulation_reform(
            connection,
            country=country,
            catalog_version_id=scope.catalog_version_id,
            choice=choice,
        ),
    )


def read_unstarted_report_run(
    connection: Connection, report_run_id: UUID
) -> StoredReportRun | None:
    """Return what the report run's coordinator call is built from, or None.

    Raise StoredRunUnreadableError when the report run names no reform, a
    side's run is missing or names no region or dataset, or its sides differ
    in catalog version, region, dataset or year; and the policy reader's
    errors when a side's values can't be read.
    """
    run = connection.execute(
        select(
            report_runs.c.created_at,
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
            report_runs.c.country_package_version,
            report_runs.c.policyengine_version,
            reports.c.country,
            reports.c.inputs,
        )
        .join(reports, reports.c.id == report_runs.c.report_id)
        .where(report_runs.c.id == report_run_id)
    ).one_or_none()
    if run is None:
        return None
    if run.reform_simulation_run_id is None:
        msg = f"report run {report_run_id} names no reform simulation run"
        raise StoredRunUnreadableError(msg)
    baseline_rows = _side_rows(connection, run.baseline_simulation_run_id)
    reform_rows = _side_rows(connection, run.reform_simulation_run_id)
    if baseline_rows is None or reform_rows is None:
        msg = f"report run {report_run_id} names a missing simulation run"
        raise StoredRunUnreadableError(msg)
    scope = _scope(baseline_rows)
    if _scope(reform_rows) != scope:
        msg = (
            f"report run {report_run_id}'s sides calculate on different catalog "
            "versions, regions, datasets or years"
        )
        raise StoredRunUnreadableError(msg)
    return StoredReportRun(
        report_run_id=report_run_id,
        country=run.country,
        versions=RunVersions(
            country_package_version=run.country_package_version,
            policyengine_version=run.policyengine_version,
        ),
        options=dict(run.inputs),
        scope=scope,
        baseline=_side(connection, run.country, scope, baseline_rows),
        reform=_side(connection, run.country, scope, reform_rows),
        created_at=run.created_at,
    )


def read_unstarted_simulation_run(
    connection: Connection, simulation_run_id: UUID
) -> StoredSimulationRun | None:
    """Return what a standalone run's simulation function call is built from.

    Return None when there is no such run. Raise StoredRunUnreadableError
    when it names no region or dataset, and the policy reader's errors when
    its values can't be read.
    """
    side_rows = _side_rows(connection, simulation_run_id)
    if side_rows is None:
        return None
    country = str(side_rows.model_name).removeprefix(MODEL_NAME_PREFIX)
    scope = _scope(side_rows)
    return StoredSimulationRun(
        country=country,
        versions=RunVersions(
            country_package_version=side_rows.country_package_version,
            policyengine_version=side_rows.policyengine_version,
        ),
        scope=scope,
        run=_side(connection, country, scope, side_rows),
        created_at=side_rows.created_at,
    )
