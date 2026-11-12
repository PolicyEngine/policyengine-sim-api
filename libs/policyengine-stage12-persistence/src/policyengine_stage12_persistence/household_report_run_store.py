"""Conditional writes to a household report run and its simulation runs.

A household report calculates one stored household under a baseline and,
optionally, a reform: each side is a household simulation run, and the
report run names them. One household function call calculates the whole
report run, as the Modal worker role. It claims the report run and each
simulation run with ``ReportRunStore``'s claims, under its own Modal call
ID, then records each run's ``HouseholdExecutionResult`` or
``HouseholdExecutionFailure`` document in ``household_result``, and marks
the report run succeeded or failed. Each write applies only while the call
still holds its row running, as every other run write does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from policyengine_stage12_persistence.report_run_store import (
    ReportRunStore,
    RunStatus,
    running_under_call,
)
from policyengine_stage12_persistence.report_run_tables import (
    report_runs,
    simulation_runs,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_stage12_persistence.report_run_store import RunWrite


class HouseholdReportRunStore(ReportRunStore):
    """A report run store that also records household simulation runs' results."""

    def require_household_report_runs(
        self,
        report_run_id: UUID,
        *,
        baseline_simulation_run_id: UUID,
        reform_simulation_run_id: UUID | None,
    ) -> None:
        """Check the report run names exactly the call's simulation runs.

        ``reform_simulation_run_id`` is None for a report of the baseline
        alone. Raises LookupError when the report run does not exist, and
        ValueError when it names other simulation runs.
        """
        statement = select(
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        ).where(report_runs.c.id == report_run_id)
        with self._transaction("require_household_report_runs") as connection:
            named = connection.execute(statement).one_or_none()
        if named is None:
            msg = f"report run {report_run_id} does not exist"
            raise LookupError(msg)
        if (
            named.baseline_simulation_run_id != baseline_simulation_run_id
            or named.reform_simulation_run_id != reform_simulation_run_id
        ):
            msg = (
                f"report run {report_run_id} names other simulation runs than "
                f"its call: baseline {named.baseline_simulation_run_id} and "
                f"reform {named.reform_simulation_run_id}"
            )
            raise ValueError(msg)

    def complete_household_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        household_result: Mapping[str, Any],
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the run succeeded with its household while this call holds it.

        ``household_result`` is the calculation's ``HouseholdExecutionResult``
        as JSON.
        """
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                running_under_call(simulation_runs, modal_call_id),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                household_result=dict(household_result),
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            simulation_runs,
            simulation_run_id,
            statement,
            "complete_household_simulation_run",
        )

    def fail_household_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
        household_result: Mapping[str, Any] | None = None,
    ) -> RunWrite:
        """Mark the run failed while this call holds it.

        ``household_result`` is the calculation's
        ``HouseholdExecutionFailure`` as JSON, or None, stored as SQL NULL,
        for a failure outside the calculation.
        """
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                running_under_call(simulation_runs, modal_call_id),
            )
            .values(
                status=RunStatus.FAILED.value,
                household_result=(
                    None if household_result is None else dict(household_result)
                ),
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            simulation_runs,
            simulation_run_id,
            statement,
            "fail_household_simulation_run",
        )

    def complete_household_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the household report run succeeded while this call holds it.

        A household report run has no results of its own: each simulation
        run holds its calculated household.
        """
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == report_run_id,
                running_under_call(report_runs, modal_call_id),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            report_runs, report_run_id, statement, "complete_household_report_run"
        )
