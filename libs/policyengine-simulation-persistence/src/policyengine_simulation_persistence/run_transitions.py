"""Conditional status changes for simulation runs and comparison steps.

Every change is one ``UPDATE ... WHERE <status> = <expected>``. When two
Simulation Entrypoint instances act on the same run at once, PostgreSQL makes
the second wait for the first's row lock, then re-checks the condition and
changes nothing. So no lease table or advisory lock is needed:

- only the instance that moves a run from ``pending`` to ``dispatching``
  starts its Modal call, and it then records the call ID (``running``);
- the runs that share one Modal call, such as a household report's baseline
  and reform, change status together, all or none;
- the recording loop claims a running call's next check by moving its
  ``next_check_at`` forward, so instances do not check the same call at once,
  and only one of them records its outcome;
- a call's results are written in the same transaction that marks its runs
  ``succeeded``, so a run never succeeds without its results;
- a run left in ``dispatching`` (the instance stopped between claiming it and
  recording its call) is failed after a while, so it can be submitted again.

An economy report run's comparison step follows the same statuses on its own
``comparison_*`` columns, and may start only once both of its simulation runs
have succeeded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import and_, exists, or_, select, update

from policyengine_simulation_persistence.run_store import utc_now
from policyengine_simulation_persistence.runs import (
    ReportRunRecord,
    RunStatus,
    SimulationRunRecord,
    bounded_error_message,
    report_runs,
    simulation_runs,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from datetime import datetime, timedelta
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, Engine

    type SimulationResultWriter = Callable[
        [Connection, tuple[SimulationRunRecord, ...]], None
    ]
    type ComparisonResultWriter = Callable[[Connection, ReportRunRecord], None]


class RunErrorCode(StrEnum):
    """Why a run or comparison step failed."""

    # Modal refused the call or never acknowledged it.
    DISPATCH_FAILED = "dispatch_failed"
    # The instance stopped between claiming the dispatch and recording the call.
    DISPATCH_ABANDONED = "dispatch_abandoned"
    # The Modal function raised.
    WORKER_ERROR = "worker_error"
    WORKER_TIMEOUT = "worker_timeout"
    # Modal no longer holds the output, or does not know the call.
    RESULT_EXPIRED = "result_expired"
    CALL_NOT_FOUND = "call_not_found"
    # The output failed validation against its result model.
    INVALID_RESULT = "invalid_result"
    # No recorder handles results of this kind of run.
    NO_RESULT_RECORDER = "no_result_recorder"
    # A comparison step whose baseline or reform simulation run failed.
    SIMULATION_RUN_FAILED = "simulation_run_failed"


@dataclass(frozen=True)
class RunError:
    """A failure to record on a run; the message must carry no user data."""

    code: RunErrorCode
    message: str


@dataclass(frozen=True)
class DueSimulationCall:
    """A running Modal call whose runs this instance claimed to check now."""

    modal_call_id: str
    runs: tuple[SimulationRunRecord, ...]


@dataclass(frozen=True)
class AbandonedDispatches:
    """Runs and comparison steps failed because their dispatch never finished."""

    simulation_run_ids: tuple[UUID, ...]
    report_run_ids: tuple[UUID, ...]


class RunTransitions:
    """Conditional status writes; each returns whether it changed anything."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for every timestamp."""
        self.engine = engine
        self._clock = clock

    # --- Simulation runs ---------------------------------------------------

    def _change_simulation_runs(
        self,
        run_ids: Collection[UUID],
        *,
        expected: RunStatus,
        values: dict[str, object],
    ) -> bool:
        """Change every run in ``run_ids`` from ``expected``, or none of them."""
        wanted = set(run_ids)
        if not wanted:
            msg = "a status change needs at least one run"
            raise ValueError(msg)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            changed = connection.execute(
                update(simulation_runs)
                .where(
                    simulation_runs.c.id.in_(wanted),
                    simulation_runs.c.status == expected.value,
                )
                .values(updated_at=self._clock(), **values)
                .returning(simulation_runs.c.id)
            ).scalars()
            if set(changed) != wanted:
                transaction.rollback()
                return False
            transaction.commit()
        return True

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        """Move pending runs to ``dispatching``; only the winner starts the call.

        ``run_ids`` are the runs one Modal call computes: one run, or a
        household report's baseline and reform. All must be pending.
        """
        return self._change_simulation_runs(
            run_ids,
            expected=RunStatus.PENDING,
            values={"status": RunStatus.DISPATCHING.value},
        )

    def record_simulation_dispatched(
        self,
        run_ids: Collection[UUID],
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the acknowledged Modal call and move the runs to ``running``."""
        return self._change_simulation_runs(
            run_ids,
            expected=RunStatus.DISPATCHING,
            values={
                "status": RunStatus.RUNNING.value,
                "modal_call_id": modal_call_id,
                "dispatched_at": self._clock(),
                "next_check_at": first_check_at,
            },
        )

    def record_simulation_dispatch_failure(
        self, run_ids: Collection[UUID], error: RunError
    ) -> bool:
        """Fail dispatching runs whose Modal call was refused."""
        return self._change_simulation_runs(
            run_ids,
            expected=RunStatus.DISPATCHING,
            values={
                "status": RunStatus.FAILED.value,
                "error_code": error.code.value,
                "error_message": bounded_error_message(error.message),
                "completed_at": self._clock(),
            },
        )

    def claim_due_simulation_calls(
        self, *, limit: int, lease: timedelta
    ) -> list[DueSimulationCall]:
        """Claim up to ``limit`` running runs that are due a check, by call.

        Each claimed run's next check moves ``lease`` ahead, so no other
        instance checks it until the lease ends; if this instance stops, the
        run becomes due again then. Rows another instance holds are skipped.
        Two runs of one call are normally claimed together; if the limit
        splits them, two instances may ask Modal about the same call, and the
        conditional writes still record its outcome once.
        """
        now = self._clock()
        due = (
            select(simulation_runs.c.id)
            .where(
                simulation_runs.c.status == RunStatus.RUNNING.value,
                simulation_runs.c.next_check_at <= now,
            )
            .order_by(simulation_runs.c.next_check_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    update(simulation_runs)
                    .where(
                        simulation_runs.c.id.in_(due),
                        simulation_runs.c.status == RunStatus.RUNNING.value,
                        simulation_runs.c.next_check_at <= now,
                    )
                    .values(
                        next_check_at=now + lease,
                        check_count=simulation_runs.c.check_count + 1,
                        updated_at=now,
                    )
                    .returning(*simulation_runs.c)
                )
                .mappings()
                .all()
            )
        runs_by_call: dict[str, list[SimulationRunRecord]] = defaultdict(list)
        for row in rows:
            run = SimulationRunRecord.from_row(row)
            if run.modal_call_id is not None:
                runs_by_call[run.modal_call_id].append(run)
        return [
            DueSimulationCall(modal_call_id=call_id, runs=tuple(runs))
            for call_id, runs in sorted(runs_by_call.items())
        ]

    def _change_simulation_call(
        self,
        connection: Connection,
        modal_call_id: str,
        values: dict[str, object],
    ) -> tuple[SimulationRunRecord, ...]:
        rows = (
            connection.execute(
                update(simulation_runs)
                .where(
                    simulation_runs.c.modal_call_id == modal_call_id,
                    simulation_runs.c.status == RunStatus.RUNNING.value,
                )
                .values(updated_at=self._clock(), **values)
                .returning(*simulation_runs.c)
            )
            .mappings()
            .all()
        )
        return tuple(SimulationRunRecord.from_row(row) for row in rows)

    def record_simulation_call_running(
        self, modal_call_id: str, *, next_check_at: datetime
    ) -> bool:
        """Set the next check of a call's runs that are still running."""
        with self.engine.begin() as connection:
            return bool(
                self._change_simulation_call(
                    connection, modal_call_id, {"next_check_at": next_check_at}
                )
            )

    def complete_simulation_call(
        self, modal_call_id: str, write_results: SimulationResultWriter
    ) -> bool:
        """Mark a call's running runs succeeded and write their results.

        ``write_results`` receives the transaction and the succeeded runs, and
        writes their result rows. If it raises, nothing is recorded and the
        exception propagates. Return False, writing nothing, when no run of
        the call is still running.
        """
        with self.engine.begin() as connection:
            runs = self._change_simulation_call(
                connection,
                modal_call_id,
                {
                    "status": RunStatus.SUCCEEDED.value,
                    "completed_at": self._clock(),
                    "next_check_at": None,
                },
            )
            if not runs:
                return False
            write_results(connection, runs)
        return True

    def fail_simulation_call(self, modal_call_id: str, error: RunError) -> bool:
        """Mark a call's running runs failed; False if none is still running."""
        with self.engine.begin() as connection:
            return bool(
                self._change_simulation_call(
                    connection,
                    modal_call_id,
                    {
                        "status": RunStatus.FAILED.value,
                        "error_code": error.code.value,
                        "error_message": bounded_error_message(error.message),
                        "completed_at": self._clock(),
                        "next_check_at": None,
                    },
                )
            )

    # --- Comparison steps of economy report runs ----------------------------

    def _change_comparison(
        self,
        connection: Connection,
        report_run_id: UUID,
        *,
        expected: RunStatus,
        values: dict[str, object],
        conditions: tuple[ColumnElement[bool], ...] = (),
    ) -> ReportRunRecord | None:
        row = (
            connection.execute(
                update(report_runs)
                .where(
                    report_runs.c.id == report_run_id,
                    report_runs.c.comparison_status == expected.value,
                    *conditions,
                )
                .values(updated_at=self._clock(), **values)
                .returning(*report_runs.c)
            )
            .mappings()
            .first()
        )
        return None if row is None else ReportRunRecord.from_row(row)

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        """Move a pending comparison step to ``dispatching``.

        Only the winner starts the comparison's Modal call. The step can start
        only once both of its simulation runs have succeeded.
        """
        baseline = simulation_runs.alias("baseline_run")
        reform = simulation_runs.alias("reform_run")
        both_runs_succeeded = (
            exists().where(
                and_(
                    baseline.c.id == report_runs.c.baseline_simulation_run_id,
                    baseline.c.status == RunStatus.SUCCEEDED.value,
                )
            ),
            exists().where(
                and_(
                    reform.c.id == report_runs.c.reform_simulation_run_id,
                    reform.c.status == RunStatus.SUCCEEDED.value,
                )
            ),
        )
        with self.engine.begin() as connection:
            return (
                self._change_comparison(
                    connection,
                    report_run_id,
                    expected=RunStatus.PENDING,
                    values={"comparison_status": RunStatus.DISPATCHING.value},
                    conditions=both_runs_succeeded,
                )
                is not None
            )

    def record_comparison_dispatched(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the comparison's acknowledged Modal call; it is now running."""
        with self.engine.begin() as connection:
            return (
                self._change_comparison(
                    connection,
                    report_run_id,
                    expected=RunStatus.DISPATCHING,
                    values={
                        "comparison_status": RunStatus.RUNNING.value,
                        "comparison_modal_call_id": modal_call_id,
                        "comparison_dispatched_at": self._clock(),
                        "comparison_next_check_at": first_check_at,
                    },
                )
                is not None
            )

    def record_comparison_dispatch_failure(
        self, report_run_id: UUID, error: RunError
    ) -> bool:
        """Fail a dispatching comparison step whose Modal call was refused."""
        with self.engine.begin() as connection:
            return (
                self._change_comparison(
                    connection,
                    report_run_id,
                    expected=RunStatus.DISPATCHING,
                    values=self._comparison_failure(error),
                )
                is not None
            )

    def claim_due_comparisons(
        self, *, limit: int, lease: timedelta
    ) -> list[ReportRunRecord]:
        """Claim up to ``limit`` running comparison steps that are due a check."""
        now = self._clock()
        due = (
            select(report_runs.c.id)
            .where(
                report_runs.c.comparison_status == RunStatus.RUNNING.value,
                report_runs.c.comparison_next_check_at <= now,
            )
            .order_by(report_runs.c.comparison_next_check_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    update(report_runs)
                    .where(
                        report_runs.c.id.in_(due),
                        report_runs.c.comparison_status == RunStatus.RUNNING.value,
                        report_runs.c.comparison_next_check_at <= now,
                    )
                    .values(
                        comparison_next_check_at=now + lease,
                        comparison_check_count=report_runs.c.comparison_check_count + 1,
                        updated_at=now,
                    )
                    .returning(*report_runs.c)
                )
                .mappings()
                .all()
            )
        return [ReportRunRecord.from_row(row) for row in rows]

    def record_comparison_running(
        self, report_run_id: UUID, *, next_check_at: datetime
    ) -> bool:
        """Set the next check of a comparison step that is still running."""
        with self.engine.begin() as connection:
            return (
                self._change_comparison(
                    connection,
                    report_run_id,
                    expected=RunStatus.RUNNING,
                    values={"comparison_next_check_at": next_check_at},
                )
                is not None
            )

    def complete_comparison(
        self, report_run_id: UUID, write_results: ComparisonResultWriter
    ) -> bool:
        """Mark a running comparison step succeeded and write its results.

        ``write_results`` runs in the same transaction; if it raises, nothing
        is recorded and the exception propagates.
        """
        with self.engine.begin() as connection:
            report_run = self._change_comparison(
                connection,
                report_run_id,
                expected=RunStatus.RUNNING,
                values={
                    "comparison_status": RunStatus.SUCCEEDED.value,
                    "comparison_completed_at": self._clock(),
                    "comparison_next_check_at": None,
                },
            )
            if report_run is None:
                return False
            write_results(connection, report_run)
        return True

    def fail_comparison(self, report_run_id: UUID, error: RunError) -> bool:
        """Mark a running comparison step failed; False if it is not running."""
        with self.engine.begin() as connection:
            return (
                self._change_comparison(
                    connection,
                    report_run_id,
                    expected=RunStatus.RUNNING,
                    values=self._comparison_failure(error),
                )
                is not None
            )

    def _comparison_failure(self, error: RunError) -> dict[str, object]:
        return {
            "comparison_status": RunStatus.FAILED.value,
            "comparison_error_code": error.code.value,
            "comparison_error_message": bounded_error_message(error.message),
            "comparison_completed_at": self._clock(),
            "comparison_next_check_at": None,
        }

    def fail_comparisons_of_failed_runs(self) -> tuple[UUID, ...]:
        """Fail pending comparison steps whose baseline or reform run failed.

        Such a step can never start, so it fails with ``simulation_run_failed``
        and its report run's status says so. Return the report runs changed.
        """
        baseline = simulation_runs.alias("failed_baseline_run")
        reform = simulation_runs.alias("failed_reform_run")
        either_run_failed = or_(
            exists().where(
                and_(
                    baseline.c.id == report_runs.c.baseline_simulation_run_id,
                    baseline.c.status == RunStatus.FAILED.value,
                )
            ),
            exists().where(
                and_(
                    reform.c.id == report_runs.c.reform_simulation_run_id,
                    reform.c.status == RunStatus.FAILED.value,
                )
            ),
        )
        failure = self._comparison_failure(
            RunError(
                RunErrorCode.SIMULATION_RUN_FAILED,
                "A simulation run this comparison needs failed.",
            )
        )
        with self.engine.begin() as connection:
            return tuple(
                connection.execute(
                    update(report_runs)
                    .where(
                        report_runs.c.comparison_status == RunStatus.PENDING.value,
                        either_run_failed,
                    )
                    .values(updated_at=self._clock(), **failure)
                    .returning(report_runs.c.id)
                ).scalars()
            )

    # --- Dispatches that never finished ------------------------------------

    def fail_abandoned_dispatches(
        self, *, claimed_before: datetime
    ) -> AbandonedDispatches:
        """Fail runs and comparison steps left in ``dispatching``.

        A run stays ``dispatching`` only if the instance that claimed it
        stopped before recording its Modal call. Failing it, with
        ``dispatch_abandoned``, lets the same simulation be run again.
        """
        now = self._clock()
        message = (
            "The instance that claimed the dispatch stopped before it recorded "
            "the Modal call."
        )
        with self.engine.begin() as connection:
            failed_runs = connection.execute(
                update(simulation_runs)
                .where(
                    simulation_runs.c.status == RunStatus.DISPATCHING.value,
                    simulation_runs.c.updated_at < claimed_before,
                )
                .values(
                    status=RunStatus.FAILED.value,
                    error_code=RunErrorCode.DISPATCH_ABANDONED.value,
                    error_message=message,
                    completed_at=now,
                    updated_at=now,
                )
                .returning(simulation_runs.c.id)
            ).scalars()
            simulation_run_ids = tuple(failed_runs)
            report_run_ids = tuple(
                connection.execute(
                    update(report_runs)
                    .where(
                        report_runs.c.comparison_status == RunStatus.DISPATCHING.value,
                        report_runs.c.updated_at < claimed_before,
                    )
                    .values(
                        updated_at=now,
                        **self._comparison_failure(
                            RunError(RunErrorCode.DISPATCH_ABANDONED, message)
                        ),
                    )
                    .returning(report_runs.c.id)
                ).scalars()
            )
        return AbandonedDispatches(
            simulation_run_ids=simulation_run_ids, report_run_ids=report_run_ids
        )
