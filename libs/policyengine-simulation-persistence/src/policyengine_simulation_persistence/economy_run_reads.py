"""Read a report or a simulation as it stands, with its newest run.

The newest run is the one created last, with ties broken by the larger ID,
whatever its status and versions, as the Public API reads it. The run's
results are the Public API's to read: these reads give only its state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    SimulationStatus,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Connection, RowMapping


@dataclass(frozen=True)
class SimulationRunState:
    """A simulation run's state: its status, versions, error and times."""

    id: UUID
    status: SimulationStatus
    country_package_version: str
    policyengine_version: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class ReportRunState:
    """A report run's state, and the simulation runs it compares."""

    id: UUID
    status: ReportRunStatus
    country_package_version: str
    policyengine_version: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID | None


@dataclass(frozen=True)
class EconomyReportState:
    """A report, its two simulations, and its newest run (None with no run)."""

    id: UUID
    country: str
    baseline_simulation_id: UUID | None
    reform_simulation_id: UUID | None
    run: ReportRunState | None


@dataclass(frozen=True)
class EconomySimulationState:
    """A simulation and its newest run (None with no run)."""

    id: UUID
    run: SimulationRunState | None


def read_economy_report(
    connection: Connection, report_id: UUID
) -> EconomyReportState | None:
    """Return the report with its newest run, or None when there is no such report."""
    report = connection.execute(
        select(
            reports.c.id,
            reports.c.country,
            reports.c.baseline_simulation_id,
            reports.c.reform_simulation_id,
        ).where(reports.c.id == report_id)
    ).one_or_none()
    if report is None:
        return None
    run = (
        connection.execute(
            select(
                report_runs.c.id,
                report_runs.c.status,
                report_runs.c.country_package_version,
                report_runs.c.policyengine_version,
                report_runs.c.error_message,
                report_runs.c.created_at,
                report_runs.c.started_at,
                report_runs.c.completed_at,
                report_runs.c.baseline_simulation_run_id,
                report_runs.c.reform_simulation_run_id,
            )
            .where(report_runs.c.report_id == report_id)
            .order_by(report_runs.c.created_at.desc(), report_runs.c.id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return EconomyReportState(
        id=report.id,
        country=report.country,
        baseline_simulation_id=report.baseline_simulation_id,
        reform_simulation_id=report.reform_simulation_id,
        run=None if run is None else _report_run_state(run),
    )


def read_economy_simulation(
    connection: Connection, simulation_id: UUID
) -> EconomySimulationState | None:
    """Return the simulation with its newest run, or None when there is none."""
    found = connection.execute(
        select(simulations.c.id).where(simulations.c.id == simulation_id)
    ).scalar_one_or_none()
    if found is None:
        return None
    run = (
        connection.execute(
            select(
                simulation_runs.c.id,
                simulation_runs.c.status,
                simulation_runs.c.country_package_version,
                simulation_runs.c.policyengine_version,
                simulation_runs.c.error_message,
                simulation_runs.c.created_at,
                simulation_runs.c.started_at,
                simulation_runs.c.completed_at,
            )
            .where(simulation_runs.c.simulation_id == simulation_id)
            .order_by(simulation_runs.c.created_at.desc(), simulation_runs.c.id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return EconomySimulationState(
        id=simulation_id,
        run=None
        if run is None
        else SimulationRunState(
            id=run["id"],
            status=SimulationStatus(run["status"]),
            country_package_version=run["country_package_version"],
            policyengine_version=run["policyengine_version"],
            error_message=run["error_message"],
            created_at=run["created_at"],
            started_at=run["started_at"],
            completed_at=run["completed_at"],
        ),
    )


def _report_run_state(run: RowMapping) -> ReportRunState:
    return ReportRunState(
        id=run["id"],
        status=ReportRunStatus(run["status"]),
        country_package_version=run["country_package_version"],
        policyengine_version=run["policyengine_version"],
        error_message=run["error_message"],
        created_at=run["created_at"],
        started_at=run["started_at"],
        completed_at=run["completed_at"],
        baseline_simulation_run_id=run["baseline_simulation_run_id"],
        reform_simulation_run_id=run["reform_simulation_run_id"],
    )
