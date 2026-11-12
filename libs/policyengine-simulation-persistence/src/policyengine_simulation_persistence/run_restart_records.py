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
   and household simulations are left out: their own tasks restart them.
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

from sqlalchemy import exists, func, or_, select, update

from policyengine_simulation_persistence.economy_run_records import (
    RunVersions,
    SimulationChoice,
)
from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    datasets,
    regions,
    tax_benefit_model_versions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.policy_reform import (
    combine_reforms,
    read_dynamic_reform,
    read_policy_reform,
)
from policyengine_simulation_persistence.tables import (
    SimulationStatus,
    SimulationType,
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


RESTART_TABLES: Final[dict[RestartKind, Table]] = {
    RestartKind.HOUSEHOLD_JOB: household_jobs,
    RestartKind.REPORT_RUN: report_runs,
    RestartKind.SIMULATION_RUN: simulation_runs,
}


@dataclass(frozen=True)
class UnstartedRun:
    """A row still pending with no Modal call, and when it was created."""

    kind: RestartKind
    run_id: UUID
    created_at: datetime


class StoredRunUnreadableError(LookupError):
    """A run's stored rows no longer describe one calculation to start."""


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
    policy_reform = (
        {}
        if choice.policy_id is None
        else read_policy_reform(
            connection,
            country=country,
            policy_id=choice.policy_id,
            catalog_version_id=scope.catalog_version_id,
        )
    )
    dynamic_reform = (
        {}
        if choice.dynamic_id is None
        else read_dynamic_reform(
            connection,
            dynamic_id=choice.dynamic_id,
            catalog_version_id=scope.catalog_version_id,
        )
    )
    return StoredEconomySide(
        simulation_run_id=side.id,
        choice=choice,
        reform=combine_reforms(policy_reform, dynamic_reform),
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
