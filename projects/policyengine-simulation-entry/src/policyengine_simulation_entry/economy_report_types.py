"""The requests, errors and store protocols of the v2 economy path.

``EconomyReports`` (``economy_reports``) runs reports through stores that
satisfy these protocols: ``SimulationStore``, ``ReportStore``, ``RunStore``,
``CatalogReader`` and ``RunTransitions`` in production, and in-memory fakes
in the tests. ``catalog_population`` turns the catalog's region and dataset
rows into the population an economy simulation call carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_simulation_contract.economy_runs import (
    CatalogPopulation,
    CatalogRowFilter,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_persistence import (
        CatalogVersion,
        NewReport,
        NewReportRun,
        NewSimulation,
        NewSimulationRun,
        OutputFileRecord,
        RegionFilter,
        RegionPopulation,
        ReportRecord,
        ReportRunRecord,
        RunError,
        SimulationKind,
        SimulationRecord,
        SimulationRunRecord,
        StartedReportRun,
        StartedSimulationRun,
    )


class EconomyRequestError(ValueError):
    """The request cannot run: unknown catalog version, region or dataset, or input."""


class ReportNotFoundError(LookupError):
    """No report has this ID."""


@dataclass(frozen=True)
class ResumedWork:
    """What one safety-check pass started of the work left waiting."""

    comparisons_started: int = 0
    runs_started: int = 0


@dataclass(frozen=True)
class SimulationChoice:
    """A simulation's policy and dynamic; neither means current law."""

    policy_id: UUID | None = None
    dynamic_id: UUID | None = None


@dataclass(frozen=True)
class EconomyReportSubmission:
    """A new economy report: two simulations of one region, a year, a version.

    ``policyengine_version`` selects the country's catalog version, which
    fixes the model, the data release and the region rows. ``dataset_name``
    names a dataset of that version; None means the region's default.
    """

    country: str
    policyengine_version: str
    year: int
    region_code: str
    baseline: SimulationChoice
    reform: SimulationChoice
    dataset_name: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


class SimulationDefinitions(Protocol):
    """``SimulationStore``'s finds and reads."""

    def find_or_create(self, new_simulation: NewSimulation) -> SimulationRecord:
        """Return the simulation with this definition, creating it if needed."""
        ...

    def get(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation, or None."""
        ...


class ReportDefinitions(Protocol):
    """``ReportStore``'s inserts and reads."""

    def create(self, new_report: NewReport) -> ReportRecord:
        """Insert a report."""
        ...

    def get(self, report_id: UUID) -> ReportRecord | None:
        """Return the report, or None."""
        ...


class EconomyRuns(Protocol):
    """``RunStore``'s finds, inserts and reads."""

    def start_simulation_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Return the reusable run, or the next attempt."""
        ...

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Insert a report run."""
        ...

    def create_first_report_run(self, new_report_run: NewReportRun) -> StartedReportRun:
        """Insert the report's first report run, or return its newest one."""
        ...

    def create_window_year_run(self, new_report_run: NewReportRun) -> StartedReportRun:
        """Insert a budget-window year's report run, or return the one added."""
        ...

    def latest_report_run(self, report_id: UUID) -> ReportRunRecord | None:
        """Return the report's newest report run, or None."""
        ...

    def get_simulation_run(self, run_id: UUID) -> SimulationRunRecord | None:
        """Return the simulation run, or None."""
        ...

    def comparisons_ready_to_start(
        self, *, simulation_run_ids: Collection[UUID] | None = None, limit: int = 100
    ) -> list[ReportRunRecord]:
        """Return pending comparisons whose runs succeeded."""
        ...

    def simulation_output_file(
        self, simulation_run_id: UUID, role: str
    ) -> OutputFileRecord | None:
        """Return a run's stored file of ``role`` while it is offered."""
        ...

    def pending_simulation_runs(
        self,
        *,
        created_before: datetime,
        simulation_kind: SimulationKind | None = None,
        limit: int = 100,
    ) -> list[SimulationRunRecord]:
        """Return runs still pending that were created before a time."""
        ...

    def report_runs_using(self, simulation_run_id: UUID) -> list[ReportRunRecord]:
        """Return the report runs comparing a simulation run."""
        ...


class EconomyCatalog(Protocol):
    """``CatalogReader``'s reads."""

    def catalog_version(
        self, country: str, policyengine_version: str
    ) -> CatalogVersion | None:
        """Return the country's catalog version for a policyengine.py version."""
        ...

    def catalog_version_by_id(self, version_id: UUID) -> CatalogVersion | None:
        """Return a catalog version by ID."""
        ...

    def population(
        self, version: CatalogVersion, region_code: str, dataset_name: str | None
    ) -> RegionPopulation:
        """Return the region and dataset rows; raise CatalogError."""
        ...

    def parameter_changes(
        self,
        catalog_version_id: UUID,
        *,
        policy_id: UUID | None,
        dynamic_id: UUID | None,
    ) -> dict[str, dict[str, Any]]:
        """Return the values a policy and dynamic set, checked against a version."""
        ...


class FailureTransitions(Protocol):
    """The ``RunTransitions`` writes that fail work that cannot start."""

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        """Move pending runs to dispatching."""
        ...

    def record_simulation_dispatch_failure(
        self, run_ids: Collection[UUID], error: RunError
    ) -> bool:
        """Fail dispatching runs."""
        ...

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        """Move a pending comparison step to dispatching."""
        ...

    def record_comparison_dispatch_failure(
        self, report_run_id: UUID, error: RunError
    ) -> bool:
        """Fail a dispatching comparison step."""
        ...


def _row_filter(region_filter: RegionFilter | None) -> CatalogRowFilter | None:
    if region_filter is None:
        return None
    return CatalogRowFilter(field=region_filter.field, value=region_filter.value)


def catalog_population(population: RegionPopulation) -> CatalogPopulation:
    """Return the population an economy simulation call carries."""
    return CatalogPopulation(
        region_code=population.region_code,
        dataset_name=population.dataset_name,
        row_filter=_row_filter(population.row_filter),
        state_code=population.state_code,
        state_filters=(
            None
            if population.state_filters is None
            else {
                code: CatalogRowFilter(field=item.field, value=item.value)
                for code, item in population.state_filters.items()
            }
        ),
    )
