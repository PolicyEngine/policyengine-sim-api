"""Builders and direct status changes for the run tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    NewSimulationRun,
    RunStore,
    RunTrigger,
    create_simulation_engine,
)
from public_api_support import superuser_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import SimulationRecord
    from public_api_support import PublicApiRows


def run_store(database) -> RunStore:
    """Return a store that connects as the runtime role."""
    return RunStore(create_simulation_engine(database.runtime_url))


def economy_run(
    simulation: SimulationRecord, rows: PublicApiRows, **overrides: object
) -> NewSimulationRun:
    """Return a 2026 run of an economy simulation on the first catalog version."""
    values: dict[str, object] = {
        "simulation_id": simulation.id,
        "tax_benefit_model_version_id": rows.catalog_version_id,
        "year": 2026,
        "trigger": RunTrigger.INITIAL,
        "region_code": "state/ca",
        "dataset_id": rows.dataset_id,
    }
    values.update(overrides)
    return NewSimulationRun(**values)  # type: ignore[arg-type]


def household_run(
    simulation: SimulationRecord, rows: PublicApiRows, **overrides: object
) -> NewSimulationRun:
    """Return a 2026 run of a household simulation on the first catalog version."""
    values: dict[str, object] = {
        "simulation_id": simulation.id,
        "tax_benefit_model_version_id": rows.catalog_version_id,
        "year": 2026,
        "trigger": RunTrigger.INITIAL,
    }
    values.update(overrides)
    return NewSimulationRun(**values)  # type: ignore[arg-type]


def set_simulation_run_status(
    database, run_id: UUID, status: str, *, completed_days_ago: int = 0
) -> None:
    """Move a run to a status directly, as the superuser, for read tests."""
    finished = status in {"succeeded", "failed"}
    engine = superuser_engine(database.superuser_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE simulation.simulation_runs SET status = :status, "
                    "modal_call_id = CASE WHEN :status IN ('running', 'succeeded') "
                    "THEN 'fc-test' ELSE modal_call_id END, "
                    "error_code = CASE WHEN :status = 'failed' "
                    "THEN 'test_failure' ELSE NULL END, "
                    "completed_at = CASE WHEN :finished "
                    "THEN now() - make_interval(days => :days) ELSE NULL END "
                    "WHERE id = :id"
                ),
                {
                    "status": status,
                    "finished": finished,
                    "days": completed_days_ago,
                    "id": run_id,
                },
            )
    finally:
        engine.dispose()
