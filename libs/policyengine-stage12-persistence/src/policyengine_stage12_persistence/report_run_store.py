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

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from policyengine_simulation_persistence.tables import (
    BudgetWindowRun,
    HouseholdJob,
    ReportRun,
    SimulationRun,
)
from sqlalchemy import and_, case, exists, func, insert, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from policyengine_stage12_persistence.engine import (
    WITHOUT_OBJECT_SYNC,
    SessionOpener,
    StoreSessions,
    open_session_once,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_execution import ReportRunIds
    from sqlalchemy import ColumnElement, Update
    from sqlalchemy.orm import Session, sessionmaker

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


type RunModel = type[ReportRun | SimulationRun | HouseholdJob]

RUN_MODELS: Final[dict[RunKind, RunModel]] = {
    RunKind.REPORT_RUN: ReportRun,
    RunKind.SIMULATION_RUN: SimulationRun,
    RunKind.HOUSEHOLD_JOB: HouseholdJob,
}
# What a missing row is called.
_ROW_NOUNS: Final[dict[RunModel, str]] = {
    ReportRun: "report run",
    SimulationRun: "simulation run",
    HouseholdJob: "household job",
}
# A bulk insert otherwise leaves out each column a row sets to None. Every row
# names the same columns, so the insert stays one statement that sets them NULL.
_EVERY_NAMED_COLUMN: Final = {"render_nulls": True}


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
    """A simulation run's status, what it calculated, its output file and its call.

    ``dataset_id`` and ``region_id`` are the Public API's catalog rows, None
    when the run names none. ``output_uri`` and ``output_sha256`` are None
    until the run records an output file, and stay None for a run that
    records none. ``modal_call_id`` is the call recorded on the run, None
    while none is; ``error_message`` is a failed run's recorded failure.
    """

    status: RunStatus
    year: int
    country_package_version: str
    policyengine_version: str
    dataset_id: UUID | None
    region_id: UUID | None
    output_uri: str | None
    output_sha256: str | None
    modal_call_id: str | None = None
    error_message: str | None = None


class ReportRunStore:
    """Status writes to the Public API's run tables and household jobs.

    It also reads a simulation run's state, for a report run that names one
    that already succeeded.
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        open_session: SessionOpener = open_session_once,
    ) -> None:
        """Use ``session_factory`` if given, else build one from ``database_url``.

        Raises ValueError when neither is given or the URL is invalid.
        """
        self.sessions = StoreSessions.for_store(
            database_url,
            session_factory=session_factory,
            open_session=open_session,
            missing_url_message="report run database URL is required",
        )

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        """Check the report run names the call's baseline and reform simulation runs.

        Raises LookupError when the report run does not exist, and ValueError
        when it names other simulation runs.
        """
        statement = select(
            ReportRun.baseline_simulation_run_id,
            ReportRun.reform_simulation_run_id,
        ).where(ReportRun.id == run_ids.report_run_id)
        with self.sessions.transaction("require_simulation_runs") as session:
            named = session.execute(statement).one_or_none()
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
        """Return a simulation run's state; see ``SimulationRunState``.

        It only reads the run. Raises LookupError when the run does not exist.
        """
        statement = select(
            SimulationRun.status,
            SimulationRun.year,
            SimulationRun.country_package_version,
            SimulationRun.policyengine_version,
            SimulationRun.dataset_id,
            SimulationRun.region_id,
            SimulationRun.output_uri,
            SimulationRun.output_sha256,
            SimulationRun.modal_call_id,
            SimulationRun.error_message,
        ).where(SimulationRun.id == simulation_run_id)
        with self.sessions.transaction("get_simulation_run") as session:
            stored = session.execute(statement).one_or_none()
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
            modal_call_id=stored.modal_call_id,
            error_message=stored.error_message,
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
            update(ReportRun)
            .where(
                ReportRun.id == report_run_id,
                ReportRun.status.in_(ACTIVE_STATUSES),
                or_(
                    ReportRun.modal_call_id.is_(None),
                    ReportRun.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(ReportRun.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(ReportRun, report_run_id, statement, "claim_report_run")

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
            update(ReportRun)
            .where(
                ReportRun.id == run_ids.report_run_id,
                running_under_call(ReportRun, modal_call_id),
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
            ReportRun,
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
            update(ReportRun)
            .where(
                ReportRun.id == report_run_id,
                running_under_call(ReportRun, modal_call_id),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(ReportRun, report_run_id, statement, "fail_report_run")

    def claim_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark the simulation run running under the worker's ``modal_call_id``.

        A pending or running run is claimed while it records no call or this
        call: the simulation service records the call it starts on a run of
        its own while the run is still pending, and Modal restarts a
        pre-empted call under the same call ID. A run recording another call
        is that call's, which every report run naming the run waits for.
        """
        statement = (
            update(SimulationRun)
            .where(
                SimulationRun.id == simulation_run_id,
                SimulationRun.status.in_(ACTIVE_STATUSES),
                or_(
                    SimulationRun.modal_call_id.is_(None),
                    SimulationRun.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(SimulationRun.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            SimulationRun, simulation_run_id, statement, "claim_simulation_run"
        )

    def complete_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        output_uri: str,
        output_sha256: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Record the run's output file and mark it succeeded while this call holds it.

        The run must be running under ``modal_call_id``, so a worker whose
        run was failed meanwhile records nothing.
        """
        statement = (
            update(SimulationRun)
            .where(
                SimulationRun.id == simulation_run_id,
                running_under_call(SimulationRun, modal_call_id),
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
            SimulationRun, simulation_run_id, statement, "complete_simulation_run"
        )

    def fail_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        error_message: str,
        failed_at: datetime,
        modal_call_id: str | None = None,
    ) -> RunWrite:
        """Mark a simulation run failed with ``error_message``.

        A worker, or the coordinator that started it, passes the worker's
        ``modal_call_id``, and fails the run only while that call holds it
        running. Without one, any pending or running run fails; no Modal call
        fails a run that way, since other report runs may share it.
        """
        held = (
            SimulationRun.status.in_(ACTIVE_STATUSES)
            if modal_call_id is None
            else running_under_call(SimulationRun, modal_call_id)
        )
        statement = (
            update(SimulationRun)
            .where(SimulationRun.id == simulation_run_id, held)
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            SimulationRun, simulation_run_id, statement, "fail_simulation_run"
        )

    def fail_simulation_run_held_by(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark a run failed while it is unfinished and still records ``modal_call_id``.

        A report run's coordinator waiting for a run another call holds fails
        it this way once that call ended without recording an outcome: the
        run may be running under it, or pending under a call the simulation
        service recorded that never claimed it. A run that finished, or that
        records another call or none, is left as it is, so coordinators
        waiting for one run fail it once.
        """
        statement = (
            update(SimulationRun)
            .where(
                SimulationRun.id == simulation_run_id,
                SimulationRun.status.in_(ACTIVE_STATUSES),
                SimulationRun.modal_call_id == modal_call_id,
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            SimulationRun,
            simulation_run_id,
            statement,
            "fail_simulation_run_held_by",
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
            update(HouseholdJob)
            .where(
                HouseholdJob.id == household_job_id,
                HouseholdJob.status.in_(ACTIVE_STATUSES),
                or_(
                    HouseholdJob.modal_call_id.is_(None),
                    HouseholdJob.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(HouseholdJob.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            HouseholdJob, household_job_id, statement, "claim_household_job"
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
            update(HouseholdJob)
            .where(
                HouseholdJob.id == household_job_id,
                running_under_call(HouseholdJob, modal_call_id),
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
            HouseholdJob, household_job_id, statement, "complete_household_job"
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
            update(HouseholdJob)
            .where(
                HouseholdJob.id == household_job_id,
                running_under_call(HouseholdJob, modal_call_id),
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
            HouseholdJob, household_job_id, statement, "fail_household_job"
        )

    def list_active_runs(
        self,
        kind: RunKind,
        *,
        active_since_before: datetime,
        regardless_of_report_runs: bool = False,
    ) -> tuple[ActiveRun, ...]:
        """Return the pending and running runs of ``kind`` active since before a time.

        A run counts from its first claim, or from its creation while it is
        pending; a household job counts from its creation, as the table's
        status and creation index allows. Runs are oldest first, with or
        without a recorded Modal call. A simulation run is listed only while
        no pending or running report run names it as its baseline or reform:
        each such report run's coordinator starts the run or waits for the
        call holding it, and fails it once that call ended without an
        outcome, so it is theirs to judge until they have finished; with
        ``regardless_of_report_runs``, for the sweep's age limit, a run that
        records a call is listed whatever report runs name it, since new ones
        may keep joining a run whose call Modal never ends. A year of a
        pending or running budget-window run that is still pending with no
        call recorded is waiting for an earlier year to end, and is listed
        only once its window has finished: the window is judged as a whole.
        A window's year counts, before its first claim, from its last change
        rather than its creation: an earlier year's coordinator may record
        its call long after the service created it, and nothing else changes
        a year that is pending with a call.
        """
        model = RUN_MODELS[kind]
        active_since = _active_since(kind)
        conditions = [
            model.status.in_(ACTIVE_STATUSES),
            active_since < active_since_before,
            _sweep_judges(kind, regardless_of_report_runs=regardless_of_report_runs),
        ]
        columns = [model.id, model.status, model.modal_call_id]
        if kind is RunKind.REPORT_RUN:
            conditions.append(~_waiting_window_year())
            columns.append(ReportRun.budget_window_run_id)
        statement = select(*columns).where(*conditions).order_by(active_since, model.id)
        with self.sessions.transaction("list_active_runs") as session:
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
                for row in session.execute(statement)
            )

    def fail_stuck_run(
        self,
        run: ActiveRun,
        *,
        error_message: str,
        failed_at: datetime,
        regardless_of_report_runs: bool = False,
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
        progress wait for this update, or this update for it. With
        ``regardless_of_report_runs``, for the sweep's age limit, a
        simulation run that records a call is failed whatever report runs
        name it.
        """
        model = RUN_MODELS[run.kind]
        same_call = (
            model.modal_call_id.is_(None)
            if run.modal_call_id is None
            else model.modal_call_id == run.modal_call_id
        )
        statement = (
            update(model)
            .where(
                model.id == run.run_id,
                model.status == run.status.value,
                same_call,
                _sweep_judges(
                    run.kind, regardless_of_report_runs=regardless_of_report_runs
                ),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(model, run.run_id, statement, "fail_stuck_run")

    def _write(
        self,
        model: RunModel,
        run_id: UUID,
        statement: Update,
        operation_name: str,
        *,
        after_change: Callable[[Session], None] | None = None,
    ) -> RunWrite:
        """Run a conditional update; return whether it changed the run, and its status.

        ``after_change`` runs in the same transaction, only after the update
        changed the run. Raises LookupError when the run does not exist.
        """
        with self.sessions.transaction(operation_name) as session:
            changed = session.execute(
                statement.returning(model.status),
                execution_options=WITHOUT_OBJECT_SYNC,
            ).scalar_one_or_none()
            if changed is not None:
                if after_change is not None:
                    after_change(session)
                return RunWrite(changed=True, status=RunStatus(changed))
            current = session.execute(
                select(model.status).where(model.id == run_id)
            ).scalar_one_or_none()
        if current is None:
            msg = f"{_ROW_NOUNS[model]} {run_id} does not exist"
            raise LookupError(msg)
        return RunWrite(changed=False, status=RunStatus(current))


def _result_inserts(result: ReportRunResult) -> Callable[[Session], None]:
    """Return the step that inserts ``result``'s rows in an open transaction."""

    def insert_rows(session: Session) -> None:
        for result_rows in result.rows:
            if not result_rows.rows:
                continue
            statement = (
                postgresql_insert(result_rows.model).on_conflict_do_nothing()
                if result_rows.shared_by_reports
                else insert(result_rows.model)
            )
            session.execute(
                statement,
                _uniform_rows(result_rows.rows),
                execution_options=_EVERY_NAMED_COLUMN,
            )

    return insert_rows


def _uniform_rows(rows: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Give every row the same columns, unset ones null, for one batched insert."""
    columns = {name for row in rows for name in row}
    return [dict.fromkeys(columns) | row for row in rows]


def running_under_call(model: RunModel, modal_call_id: str) -> ColumnElement[bool]:
    """Return the condition that a row is running under ``modal_call_id``.

    A run or household job is completed or failed by a call only while that
    call holds it running.
    """
    return and_(
        model.status == RunStatus.RUNNING.value,
        model.modal_call_id == modal_call_id,
    )


def _active_since(kind: RunKind) -> ColumnElement[datetime]:
    """Return when a run of ``kind`` became active, for the sweep's ages.

    A household job counts from its creation. A run counts from its first
    claim or, before it, from its creation, except a budget window's year,
    which counts from its last change: when its call was recorded.
    """
    if kind is RunKind.HOUSEHOLD_JOB:
        return HouseholdJob.created_at
    if kind is RunKind.SIMULATION_RUN:
        return func.coalesce(SimulationRun.started_at, SimulationRun.created_at)
    unclaimed_since = case(
        (ReportRun.budget_window_run_id.is_not(None), ReportRun.updated_at),
        else_=ReportRun.created_at,
    )
    return func.coalesce(ReportRun.started_at, unclaimed_since)


def _waiting_window_year() -> ColumnElement[bool]:
    """Return the condition that a report run is a year waiting in its window.

    It is pending with no call recorded, in a pending or running budget-window
    run, so an earlier year's coordinator will start it.
    """
    return and_(
        ReportRun.status == RunStatus.PENDING.value,
        ReportRun.modal_call_id.is_(None),
        exists().where(
            BudgetWindowRun.id == ReportRun.budget_window_run_id,
            BudgetWindowRun.status.in_(ACTIVE_STATUSES),
        ),
    )


def _sweep_judges(
    kind: RunKind, *, regardless_of_report_runs: bool
) -> ColumnElement[bool]:
    """Return the condition that the sweep may judge a run of ``kind``.

    A simulation run must have no pending or running report run naming it;
    for the age limit, one that records a call needs none. One no call has
    claimed has no call to outlast: it waits for the report runs naming it.
    """
    if kind is not RunKind.SIMULATION_RUN:
        return true()
    if not regardless_of_report_runs:
        return no_active_report_run_names()
    return or_(SimulationRun.modal_call_id.is_not(None), no_active_report_run_names())


def no_active_report_run_names() -> ColumnElement[bool]:
    """Return the condition that no pending or running report run names the run.

    A report run names its simulation runs as its baseline and its reform.
    """
    return ~exists().where(
        or_(
            ReportRun.baseline_simulation_run_id == SimulationRun.id,
            ReportRun.reform_simulation_run_id == SimulationRun.id,
        ),
        ReportRun.status.in_(ACTIVE_STATUSES),
    )
