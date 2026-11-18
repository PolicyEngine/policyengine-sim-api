"""Read a report or a simulation as it stands, with its newest run.

The newest run is the one created last, with ties broken by the larger ID,
whatever its status and versions, as the Public API reads it. The run's
results are API v1's to read: these reads give only its state. Each read
selects the mapped classes' attributes, so it returns what the database holds
now, whatever the session has loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from policyengine_simulation_persistence.tables import (
    Report,
    ReportRun,
    ReportRunStatus,
    Simulation,
    SimulationRun,
    SimulationStatus,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import RowMapping
    from sqlalchemy.orm import Session


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


def read_economy_report(session: Session, report_id: UUID) -> EconomyReportState | None:
    """Return the report with its newest run, or None when there is no such report."""
    report = session.execute(
        select(
            Report.id,
            Report.country,
            Report.baseline_simulation_id,
            Report.reform_simulation_id,
        ).where(Report.id == report_id)
    ).one_or_none()
    if report is None:
        return None
    run = (
        session.execute(
            select(
                ReportRun.id,
                ReportRun.status,
                ReportRun.country_package_version,
                ReportRun.policyengine_version,
                ReportRun.error_message,
                ReportRun.created_at,
                ReportRun.started_at,
                ReportRun.completed_at,
                ReportRun.baseline_simulation_run_id,
                ReportRun.reform_simulation_run_id,
            )
            .where(ReportRun.report_id == report_id)
            .order_by(ReportRun.created_at.desc(), ReportRun.id.desc())
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
    session: Session, simulation_id: UUID
) -> EconomySimulationState | None:
    """Return the simulation with its newest run, or None when there is none."""
    found = session.execute(
        select(Simulation.id).where(Simulation.id == simulation_id)
    ).scalar_one_or_none()
    if found is None:
        return None
    run = (
        session.execute(
            select(
                SimulationRun.id,
                SimulationRun.status,
                SimulationRun.country_package_version,
                SimulationRun.policyengine_version,
                SimulationRun.error_message,
                SimulationRun.created_at,
                SimulationRun.started_at,
                SimulationRun.completed_at,
            )
            .where(SimulationRun.simulation_id == simulation_id)
            .order_by(SimulationRun.created_at.desc(), SimulationRun.id.desc())
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
