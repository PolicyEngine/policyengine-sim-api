"""Conditional writes of run and household job status, as the Modal worker role.

Every status write is one ``UPDATE`` conditional on the run's expected
status, so a call that Modal restarts after pre-emption, or a second writer,
changes nothing that is already final. A report run's completion sets its own
result columns in the same update, and inserts its result rows in the same
transaction, only when its update changed the run. A household job is
written the same way, and its result document is set in the update that
finishes it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from policyengine_stage12_persistence.report_run_tables import (
    budget_window_runs,
    household_jobs,
    report_runs,
    simulation_runs,
)
from policyengine_stage12_persistence.store import (
    ConnectionOpener,
    create_stage12_engine,
    open_connection_once,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_execution import ReportRunIds
    from sqlalchemy import ColumnElement, Engine, Table, Update
    from sqlalchemy.engine import Connection

    from policyengine_stage12_persistence.report_run_results import ReportRunResult


class RunStatus(StrEnum):
    """A report run's, simulation run's or household job's status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


class RunKind(StrEnum):
    """Which run table a run, or a household job, is in."""

    REPORT_RUN = "report_run"
    SIMULATION_RUN = "simulation_run"
    HOUSEHOLD_JOB = "household_job"


RUN_TABLES = {
    RunKind.REPORT_RUN: report_runs,
    RunKind.SIMULATION_RUN: simulation_runs,
    RunKind.HOUSEHOLD_JOB: household_jobs,
}
# What a missing row is called, by table name.
_ROW_NOUNS = {
    report_runs.name: "report run",
    simulation_runs.name: "simulation run",
    household_jobs.name: "household job",
}


@dataclass(frozen=True)
class ActiveRun:
    """A pending or running run or household job, and its recorded Modal call.

    ``modal_call_id`` is None for a run with no call recorded.
    ``budget_window_run_id`` is the budget-window run a report run is a year
    of, and None for any other run.
    """

    kind: RunKind
    run_id: UUID
    status: RunStatus
    modal_call_id: str | None
    budget_window_run_id: UUID | None = None


@dataclass(frozen=True)
class RunWrite:
    """One conditional write's outcome: whether it changed the run, and its status."""

    changed: bool
    status: RunStatus


@dataclass(frozen=True)
class SimulationRunState:
    """A simulation run's status, what it calculated, and its output file.

    ``dataset_id`` and ``region_id`` are the Public API's catalog rows, None
    when the run names none. ``output_uri`` and ``output_sha256`` are None
    until the run records an output file, and stay None for a run that
    records none.
    """

    status: RunStatus
    year: int
    country_package_version: str
    policyengine_version: str
    dataset_id: UUID | None
    region_id: UUID | None
    output_uri: str | None
    output_sha256: str | None


class ReportRunStore:
    """Status writes to the Public API's run tables and household jobs.

    It also reads a simulation run's state, for a report run that names one
    that already succeeded.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        open_connection: ConnectionOpener = open_connection_once,
    ) -> None:
        """Use ``engine`` if given, else build one from ``database_url``.

        Raises ValueError when neither is given or the URL is invalid.
        """
        if engine is None:
            if database_url is None:
                msg = "report run database URL is required"
                raise ValueError(msg)
            engine = create_stage12_engine(database_url)
        self.engine = engine
        self._open_connection = open_connection

    @contextmanager
    def _transaction(self, operation_name: str) -> Iterator[Connection]:
        # As in the Stage 12 store, the opener only opens the connection; every
        # statement and the COMMIT run after it returns.
        connection = self._open_connection(
            self.engine.connect,
            operation_name=operation_name,
        )
        with connection, connection.begin():
            yield connection

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        """Check the report run names the call's baseline and reform simulation runs.

        Raises LookupError when the report run does not exist, and ValueError
        when it names other simulation runs.
        """
        statement = select(
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        ).where(report_runs.c.id == run_ids.report_run_id)
        with self._transaction("require_simulation_runs") as connection:
            named = connection.execute(statement).one_or_none()
        if named is None:
            msg = f"report run {run_ids.report_run_id} does not exist"
            raise LookupError(msg)
        if (
            named.baseline_simulation_run_id != run_ids.baseline_simulation_run_id
            or named.reform_simulation_run_id != run_ids.reform_simulation_run_id
        ):
            msg = (
                f"report run {run_ids.report_run_id} names other simulation runs "
                f"than its call: baseline {named.baseline_simulation_run_id} and "
                f"reform {named.reform_simulation_run_id}"
            )
            raise ValueError(msg)

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        """Return a simulation run's status, year, versions, dataset, region and output.

        It only reads the run. Raises LookupError when the run does not exist.
        """
        statement = select(
            simulation_runs.c.status,
            simulation_runs.c.year,
            simulation_runs.c.country_package_version,
            simulation_runs.c.policyengine_version,
            simulation_runs.c.dataset_id,
            simulation_runs.c.region_id,
            simulation_runs.c.output_uri,
            simulation_runs.c.output_sha256,
        ).where(simulation_runs.c.id == simulation_run_id)
        with self._transaction("get_simulation_run") as connection:
            stored = connection.execute(statement).one_or_none()
        if stored is None:
            msg = f"simulation run {simulation_run_id} does not exist"
            raise LookupError(msg)
        return SimulationRunState(
            status=RunStatus(stored.status),
            year=stored.year,
            country_package_version=stored.country_package_version,
            policyengine_version=stored.policyengine_version,
            dataset_id=stored.dataset_id,
            region_id=stored.region_id,
            output_uri=stored.output_uri,
            output_sha256=stored.output_sha256,
        )

    def claim_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark the report run running under ``modal_call_id``.

        A pending or running run is claimed while it records no call or this
        call: the simulation service records the call it starts while the run
        is still pending, and Modal restarts a pre-empted call under the same
        call ID. A run recording another call is that call's.
        """
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == report_run_id,
                report_runs.c.status.in_(ACTIVE_STATUSES),
                or_(
                    report_runs.c.modal_call_id.is_(None),
                    report_runs.c.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(report_runs.c.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(report_runs, report_run_id, statement, "claim_report_run")

    def complete_report_run(
        self,
        run_ids: ReportRunIds,
        *,
        modal_call_id: str,
        completed_at: datetime,
        result: ReportRunResult | None = None,
    ) -> RunWrite:
        """Mark the report run succeeded while ``modal_call_id`` holds it running.

        The same update sets ``result``'s columns of the report run, and its
        rows are inserted in the same transaction, only when the status
        changed, so a repeated completion stores nothing twice and a failed
        insert leaves the run running without them. A row of a simulation run
        that already has one for the same measure, from a report run sharing
        that simulation run, keeps the stored row.
        """
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == run_ids.report_run_id,
                _running_under_call(report_runs, modal_call_id),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
                **({} if result is None else result.report_run_columns),
            )
        )
        return self._write(
            report_runs,
            run_ids.report_run_id,
            statement,
            "complete_report_run",
            after_change=None if result is None else _result_inserts(result),
        )

    def fail_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the report run failed while ``modal_call_id`` holds it running."""
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == report_run_id,
                _running_under_call(report_runs, modal_call_id),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(report_runs, report_run_id, statement, "fail_report_run")

    def claim_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark a pending or running simulation run running under ``modal_call_id``.

        A running one moves to the newest call, because the coordinator starts
        a simulation again only after stopping its earlier call.
        """
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(simulation_runs.c.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "claim_simulation_run"
        )

    def complete_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        output_uri: str,
        output_sha256: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Record a running simulation run's output file and mark it succeeded."""
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status == RunStatus.RUNNING.value,
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                output_uri=output_uri,
                output_sha256=output_sha256,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "complete_simulation_run"
        )

    def fail_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark a pending or running simulation run failed with ``error_message``."""
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "fail_simulation_run"
        )

    def claim_household_job(
        self,
        household_job_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark the household job running under ``modal_call_id``.

        A pending or running job is claimed while it records no call or this
        call: the simulation service records the call it starts while the job
        is still pending, and Modal restarts a pre-empted call under the same
        call ID. A job recording another call is that call's.
        """
        statement = (
            update(household_jobs)
            .where(
                household_jobs.c.id == household_job_id,
                household_jobs.c.status.in_(ACTIVE_STATUSES),
                or_(
                    household_jobs.c.modal_call_id.is_(None),
                    household_jobs.c.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(household_jobs.c.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            household_jobs, household_job_id, statement, "claim_household_job"
        )

    def complete_household_job(
        self,
        household_job_id: UUID,
        *,
        modal_call_id: str,
        result: Mapping[str, Any],
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the household job succeeded with ``result`` while this call holds it.

        ``result`` is the calculation's ``HouseholdExecutionResult`` as JSON.
        The job must be running under ``modal_call_id``.
        """
        statement = (
            update(household_jobs)
            .where(
                household_jobs.c.id == household_job_id,
                _running_under_call(household_jobs, modal_call_id),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                result=dict(result),
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            household_jobs, household_job_id, statement, "complete_household_job"
        )

    def fail_household_job(
        self,
        household_job_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
        result: Mapping[str, Any] | None = None,
    ) -> RunWrite:
        """Mark the household job failed while ``modal_call_id`` holds it running.

        ``result`` is the calculation's ``HouseholdExecutionFailure`` as JSON,
        or None, which leaves the result null, for an error outside the
        calculation. ``error_message`` is the failure's one-line summary.
        """
        statement = (
            update(household_jobs)
            .where(
                household_jobs.c.id == household_job_id,
                _running_under_call(household_jobs, modal_call_id),
            )
            .values(
                status=RunStatus.FAILED.value,
                result=None if result is None else dict(result),
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            household_jobs, household_job_id, statement, "fail_household_job"
        )

    def list_active_runs(
        self,
        kind: RunKind,
        *,
        active_since_before: datetime,
    ) -> tuple[ActiveRun, ...]:
        """Return the pending and running runs of ``kind`` active since before a time.

        A run counts from its first claim, or from its creation while it is
        pending; a household job counts from its creation, as the table's
        status and creation index allows. Runs are oldest first, with or
        without a recorded Modal call. A simulation run is listed only while
        no pending or running report run names it as its baseline or reform:
        that report run's coordinator starts its children's calls, records
        their outcomes, and stops and starts them again when Modal restarts
        it, so they are its to judge until it has finished. A year of a
        pending or running budget-window run that is still pending with no
        call recorded is waiting for an earlier year to end, and is listed
        only once its window has finished: the window is judged as a whole.
        A window's year counts, before its first claim, from its last change
        rather than its creation: an earlier year's coordinator may record
        its call long after the service created it, and nothing else changes
        a year that is pending with a call.
        """
        table = RUN_TABLES[kind]
        active_since = _active_since(kind)
        conditions = [
            table.c.status.in_(ACTIVE_STATUSES),
            active_since < active_since_before,
        ]
        columns = [table.c.id, table.c.status, table.c.modal_call_id]
        if kind is RunKind.SIMULATION_RUN:
            conditions.append(no_active_report_run_names(table))
        if kind is RunKind.REPORT_RUN:
            conditions.append(~_waiting_window_year())
            columns.append(table.c.budget_window_run_id)
        statement = (
            select(*columns).where(*conditions).order_by(active_since, table.c.id)
        )
        with self._transaction("list_active_runs") as connection:
            return tuple(
                ActiveRun(
                    kind=kind,
                    run_id=row.id,
                    status=RunStatus(row.status),
                    modal_call_id=row.modal_call_id,
                    budget_window_run_id=(
                        row.budget_window_run_id if kind is RunKind.REPORT_RUN else None
                    ),
                )
                for row in connection.execute(statement)
            )

    def fail_stuck_run(
        self,
        run: ActiveRun,
        *,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark a stuck ``run`` failed, while it is as it was listed.

        It must still have its listed status and ``run.modal_call_id``, or no
        call if it was listed with none, and a simulation run must still have
        no pending or running report run naming it. A run that was claimed,
        finished, or moved to another call, that the simulation service
        recorded a call on, or that a pending or running report run has named
        since it was listed, is left as it is, so concurrent sweeps change it
        once. A report run naming it counts once its insert has committed,
        before this update starts: the database doesn't make an insert in
        progress wait for this update, or this update for it.
        """
        table = RUN_TABLES[run.kind]
        same_call = (
            table.c.modal_call_id.is_(None)
            if run.modal_call_id is None
            else table.c.modal_call_id == run.modal_call_id
        )
        statement = (
            update(table)
            .where(
                table.c.id == run.run_id,
                table.c.status == run.status.value,
                same_call,
                *(
                    (no_active_report_run_names(table),)
                    if run.kind is RunKind.SIMULATION_RUN
                    else ()
                ),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(table, run.run_id, statement, "fail_stuck_run")

    def _write(
        self,
        table: Table,
        run_id: UUID,
        statement: Update,
        operation_name: str,
        *,
        after_change: Callable[[Connection], None] | None = None,
    ) -> RunWrite:
        """Run a conditional update; return whether it changed the run, and its status.

        ``after_change`` runs in the same transaction, only after the update
        changed the run. Raises LookupError when the run does not exist.
        """
        with self._transaction(operation_name) as connection:
            changed = connection.execute(
                statement.returning(table.c.status)
            ).scalar_one_or_none()
            if changed is not None:
                if after_change is not None:
                    after_change(connection)
                return RunWrite(changed=True, status=RunStatus(changed))
            current = connection.execute(
                select(table.c.status).where(table.c.id == run_id)
            ).scalar_one_or_none()
        if current is None:
            msg = f"{_ROW_NOUNS[table.name]} {run_id} does not exist"
            raise LookupError(msg)
        return RunWrite(changed=False, status=RunStatus(current))


def _result_inserts(result: ReportRunResult) -> Callable[[Connection], None]:
    """Return the step that inserts ``result``'s rows on an open transaction."""

    def insert_rows(connection: Connection) -> None:
        for result_rows in result.rows:
            if not result_rows.rows:
                continue
            statement = (
                postgresql_insert(result_rows.table).on_conflict_do_nothing()
                if result_rows.shared_by_reports
                else insert(result_rows.table)
            )
            connection.execute(statement, _uniform_rows(result_rows.rows))

    return insert_rows


def _uniform_rows(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Give every row the same columns, unset ones null, for one batched insert."""
    columns = {name for row in rows for name in row}
    return [dict.fromkeys(columns) | row for row in rows]


def _running_under_call(table: Table, modal_call_id: str) -> ColumnElement[bool]:
    """Return the condition that a row is running under ``modal_call_id``.

    A report run or household job is completed or failed by a call only while
    that call holds it running.
    """
    return and_(
        table.c.status == RunStatus.RUNNING.value,
        table.c.modal_call_id == modal_call_id,
    )


def _active_since(kind: RunKind) -> ColumnElement[datetime]:
    """Return when a run of ``kind`` became active, for the sweep's ages.

    A household job counts from its creation. A run counts from its first
    claim or, before it, from its creation, except a budget window's year,
    which counts from its last change: when its call was recorded.
    """
    table = RUN_TABLES[kind]
    if kind is RunKind.HOUSEHOLD_JOB:
        return table.c.created_at
    unclaimed_since = (
        case(
            (table.c.budget_window_run_id.is_not(None), table.c.updated_at),
            else_=table.c.created_at,
        )
        if kind is RunKind.REPORT_RUN
        else table.c.created_at
    )
    return func.coalesce(table.c.started_at, unclaimed_since)


def _waiting_window_year() -> ColumnElement[bool]:
    """Return the condition that a report run is a year waiting in its window.

    It is pending with no call recorded, in a pending or running budget-window
    run, so an earlier year's coordinator will start it.
    """
    return and_(
        report_runs.c.status == RunStatus.PENDING.value,
        report_runs.c.modal_call_id.is_(None),
        exists().where(
            budget_window_runs.c.id == report_runs.c.budget_window_run_id,
            budget_window_runs.c.status.in_(ACTIVE_STATUSES),
        ),
    )


def no_active_report_run_names(table: Table) -> ColumnElement[bool]:
    """Return the condition that no pending or running report run names the run.

    A report run names its simulation runs as its baseline and its reform.
    """
    return ~exists().where(
        or_(
            report_runs.c.baseline_simulation_run_id == table.c.id,
            report_runs.c.reform_simulation_run_id == table.c.id,
        ),
        report_runs.c.status.in_(ACTIVE_STATUSES),
    )
