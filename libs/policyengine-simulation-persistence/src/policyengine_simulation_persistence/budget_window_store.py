"""Create, read, schedule and finish budget-window runs, and read their years.

A budget-window run starts ``running``. Whoever starts its next years first
claims it (``claim_scheduling``): a claim is a lease, so a caller that stopped
loses it once the lease ends, and another caller can carry on. A caller
releases only its own claim, named by the time it lasts until, so a caller
that finishes after its lease ended leaves the newer claim alone; releasing
sets when the safety check next looks at the run. The run is finished
once, with a conditional write, as ``succeeded`` or ``failed``.

A run's years are its report runs, read with both simulation runs of each.
Its budget figures are each year's ``budget_impacts`` row, read in year order,
so a reader sums them; nothing stores a total.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.budget_window_runs import (
    FINISHED_BUDGET_WINDOW_STATUSES,
    BudgetWindowRunRecord,
    BudgetWindowStatus,
    BudgetWindowYear,
    StartedBudgetWindowRun,
    YearBudget,
    budget_window_runs,
)
from policyengine_simulation_persistence.reports import reports
from policyengine_simulation_persistence.result_tables import budget_impacts
from policyengine_simulation_persistence.run_store import utc_now
from policyengine_simulation_persistence.runs import (
    ReportRunRecord,
    SimulationRunRecord,
    bounded_error_message,
    report_runs,
    simulation_runs,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, Engine

    from policyengine_simulation_persistence.budget_window_runs import (
        NewBudgetWindowRun,
    )
    from policyengine_simulation_persistence.run_transitions import RunError

# The first of the two keys of the transaction-level advisory locks that
# serialise a report's first budget-window runs; the second is the hash of
# the report ID. ``RunStore`` uses 2_020_101 for first report runs.
FIRST_BUDGET_WINDOW_RUN_LOCK_SPACE = 2_030_101


class BudgetWindowRunStore:
    """Typed SQLAlchemy Core reads and writes for budget-window runs."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for every timestamp."""
        self.engine = engine
        self._clock = clock

    # --- Creating runs -----------------------------------------------------

    def create_run(
        self, new_run: NewBudgetWindowRun, *, first_check_at: datetime
    ) -> BudgetWindowRunRecord:
        """Add a running budget-window run, due its first check at a time.

        Raise LookupError when the report does not exist, and ValueError
        when it is not a budget-window report.
        """
        with self.engine.begin() as connection:
            return self._insert(connection, new_run, first_check_at)

    def create_first_run(
        self, new_run: NewBudgetWindowRun, *, first_check_at: datetime
    ) -> StartedBudgetWindowRun:
        """Add the report's first budget-window run, or return its newest one.

        Callers starting a report's first run at the same time wait for each
        other on a transaction-level advisory lock on the report, so only the
        first adds a run. Raise as ``create_run`` does.
        """
        with self.engine.begin() as connection:
            connection.execute(
                select(
                    func.pg_advisory_xact_lock(
                        FIRST_BUDGET_WINDOW_RUN_LOCK_SPACE,
                        func.hashtext(str(new_run.report_id)),
                    )
                )
            )
            newest = self._newest(
                connection, budget_window_runs.c.report_id == new_run.report_id
            )
            if newest is not None:
                return StartedBudgetWindowRun(run=newest, created=False)
            return StartedBudgetWindowRun(
                run=self._insert(connection, new_run, first_check_at), created=True
            )

    def _insert(
        self,
        connection: Connection,
        new_run: NewBudgetWindowRun,
        first_check_at: datetime,
    ) -> BudgetWindowRunRecord:
        window_size = connection.execute(
            select(reports.c.id, reports.c.budget_window_size).where(
                reports.c.id == new_run.report_id
            )
        ).first()
        if window_size is None:
            msg = f"no report {new_run.report_id}"
            raise LookupError(msg)
        if window_size.budget_window_size is None:
            msg = f"report {new_run.report_id} is not a budget-window report"
            raise ValueError(msg)
        now = self._clock()
        row = (
            connection.execute(
                postgres_insert(budget_window_runs)
                .values(
                    id=uuid4(),
                    report_id=new_run.report_id,
                    tax_benefit_model_version_id=new_run.tax_benefit_model_version_id,
                    trigger=new_run.trigger.value,
                    force=new_run.force,
                    max_concurrent_years=new_run.max_concurrent_years,
                    status=BudgetWindowStatus.RUNNING.value,
                    next_check_at=first_check_at,
                    created_at=now,
                    updated_at=now,
                )
                .returning(*budget_window_runs.c)
            )
            .mappings()
            .one()
        )
        return BudgetWindowRunRecord.from_row(row)

    # --- Reading runs ------------------------------------------------------

    def get_run(self, run_id: UUID) -> BudgetWindowRunRecord | None:
        """Return the budget-window run with this ID, or None."""
        with self.engine.connect() as connection:
            return self._newest(connection, budget_window_runs.c.id == run_id)

    def latest_run(self, report_id: UUID) -> BudgetWindowRunRecord | None:
        """Return the report's newest budget-window run: the report's status."""
        with self.engine.connect() as connection:
            return self._newest(connection, budget_window_runs.c.report_id == report_id)

    def runs(self, report_id: UUID) -> list[BudgetWindowRunRecord]:
        """Return the report's budget-window runs, newest first."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(budget_window_runs)
                .where(budget_window_runs.c.report_id == report_id)
                .order_by(
                    budget_window_runs.c.created_at.desc(),
                    budget_window_runs.c.id.desc(),
                )
            ).mappings()
            return [BudgetWindowRunRecord.from_row(row) for row in rows]

    @staticmethod
    def _newest(
        connection: Connection, condition: ColumnElement[bool]
    ) -> BudgetWindowRunRecord | None:
        row = (
            connection.execute(
                select(budget_window_runs)
                .where(condition)
                .order_by(
                    budget_window_runs.c.created_at.desc(),
                    budget_window_runs.c.id.desc(),
                )
                .limit(1)
            )
            .mappings()
            .first()
        )
        return None if row is None else BudgetWindowRunRecord.from_row(row)

    def due_runs(self, *, now: datetime, limit: int) -> list[BudgetWindowRunRecord]:
        """Return running runs whose next check is due, the longest-due first."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(budget_window_runs)
                .where(
                    budget_window_runs.c.status == BudgetWindowStatus.RUNNING.value,
                    budget_window_runs.c.next_check_at <= now,
                )
                .order_by(
                    budget_window_runs.c.next_check_at,
                    budget_window_runs.c.created_at,
                    budget_window_runs.c.id,
                )
                .limit(limit)
            ).mappings()
            return [BudgetWindowRunRecord.from_row(row) for row in rows]

    # --- A run's years -----------------------------------------------------

    def years(self, run_id: UUID) -> list[BudgetWindowYear]:
        """Return the years the run has started, in year order, with their runs."""
        with self.engine.connect() as connection:
            year_runs = [
                ReportRunRecord.from_row(row)
                for row in connection.execute(
                    select(report_runs)
                    .where(report_runs.c.budget_window_run_id == run_id)
                    .order_by(report_runs.c.year)
                ).mappings()
            ]
            simulation_run_ids = {
                run_id
                for year_run in year_runs
                for run_id in (
                    year_run.baseline_simulation_run_id,
                    year_run.reform_simulation_run_id,
                )
                if run_id is not None
            }
            by_id = {
                row["id"]: SimulationRunRecord.from_row(row)
                for row in connection.execute(
                    select(simulation_runs).where(
                        simulation_runs.c.id.in_(simulation_run_ids)
                    )
                ).mappings()
            }
        return [
            BudgetWindowYear(
                report_run=year_run,
                baseline_run=by_id[year_run.baseline_simulation_run_id],
                reform_run=by_id[
                    year_run.reform_simulation_run_id
                    or year_run.baseline_simulation_run_id
                ],
            )
            for year_run in year_runs
        ]

    def year_budgets(self, run_id: UUID) -> list[YearBudget]:
        """Return the budget figures of each year whose comparison stored them."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    report_runs.c.year,
                    budget_impacts.c.budgetary_impact,
                    budget_impacts.c.tax_revenue_impact,
                    budget_impacts.c.state_tax_revenue_impact,
                    budget_impacts.c.benefit_spending_impact,
                )
                .join(
                    budget_impacts, budget_impacts.c.report_run_id == report_runs.c.id
                )
                .where(report_runs.c.budget_window_run_id == run_id)
                .order_by(report_runs.c.year)
            ).mappings()
            return [YearBudget(**row) for row in rows]

    # --- Scheduling and finishing ------------------------------------------

    def claim_scheduling(self, run_id: UUID, *, lease: timedelta) -> datetime | None:
        """Claim a running run to start its next years.

        Return when the claim ends, which names it for ``release_scheduling``,
        or None when another caller holds a claim whose lease has not ended,
        or the run has finished.
        """
        now = self._clock()
        claimed_until = now + lease
        with self.engine.begin() as connection:
            claimed = connection.execute(
                update(budget_window_runs)
                .where(
                    budget_window_runs.c.id == run_id,
                    budget_window_runs.c.status == BudgetWindowStatus.RUNNING.value,
                    or_(
                        budget_window_runs.c.scheduling_claimed_until.is_(None),
                        budget_window_runs.c.scheduling_claimed_until <= now,
                    ),
                )
                .values(scheduling_claimed_until=claimed_until, updated_at=now)
            )
        return claimed_until if claimed.rowcount == 1 else None

    def release_scheduling(
        self, run_id: UUID, *, claimed_until: datetime, next_check_at: datetime
    ) -> None:
        """Release this caller's claim on a running run and set its next check.

        ``claimed_until`` is what ``claim_scheduling`` returned. A claim
        another caller took after this one's lease ended is left in place.
        """
        now = self._clock()
        with self.engine.begin() as connection:
            connection.execute(
                update(budget_window_runs)
                .where(
                    budget_window_runs.c.id == run_id,
                    budget_window_runs.c.status == BudgetWindowStatus.RUNNING.value,
                    budget_window_runs.c.scheduling_claimed_until == claimed_until,
                )
                .values(
                    scheduling_claimed_until=None,
                    next_check_at=next_check_at,
                    updated_at=now,
                )
            )

    def finish_run(
        self,
        run_id: UUID,
        status: BudgetWindowStatus,
        error: RunError | None = None,
    ) -> bool:
        """Mark a running run succeeded, or failed with its error; return if it was.

        Raise ValueError for a status that does not finish a run, a failure
        without its error, or a success with one.
        """
        if status not in FINISHED_BUDGET_WINDOW_STATUSES:
            msg = "a budget-window run is finished as succeeded or failed"
            raise ValueError(msg)
        if (status is BudgetWindowStatus.FAILED) != (error is not None):
            msg = "a failed budget-window run, and only a failed one, has an error"
            raise ValueError(msg)
        now = self._clock()
        with self.engine.begin() as connection:
            finished = connection.execute(
                update(budget_window_runs)
                .where(
                    budget_window_runs.c.id == run_id,
                    budget_window_runs.c.status == BudgetWindowStatus.RUNNING.value,
                )
                .values(
                    status=status.value,
                    error_code=None if error is None else error.code.value,
                    error_message=(
                        None if error is None else bounded_error_message(error.message)
                    ),
                    completed_at=now,
                    next_check_at=None,
                    scheduling_claimed_until=None,
                    updated_at=now,
                )
            )
        return finished.rowcount == 1
