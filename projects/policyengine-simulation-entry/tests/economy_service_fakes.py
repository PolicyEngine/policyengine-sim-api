"""Fakes of the economy submission service's store, manifest and Modal calls.

The store's own reads and writes are tested against PostgreSQL in the
persistence library; here a fake records what the service asks of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any
from uuid import UUID

from economy_fixtures import economy_manifest
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportRequest,
    EconomySimulationRequest,
)
from policyengine_simulation_entry.economy_submissions import (
    EconomySubmissionService,
    ServiceSettings,
)
from policyengine_simulation_entry.economy_workers import (
    EconomyWorker,
    default_economy_worker,
)
from policyengine_simulation_persistence import (
    CatalogDataset,
    CatalogRegion,
    EconomyCatalogRows,
    EconomyReportInputs,
    EconomyReportState,
    EconomySimulationInputs,
    EconomySimulationState,
    ReportRunStatus,
    ReusableRun,
    SimulationInputs,
    SimulationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policyengine_simulation_contract.stage12_manifest import V2WorkerVersion
    from policyengine_simulation_persistence import (
        EconomyRequest,
        NewEconomyReport,
        NewEconomySimulation,
        RunReuse,
        SimulationChoice,
    )

CATALOG_VERSION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
MODEL_ID = UUID("00000000-0000-4000-8000-0000000000c2")
REGION_ID = UUID("00000000-0000-4000-8000-0000000000c3")
DATASET_ID = UUID("00000000-0000-4000-8000-0000000000c4")
POLICY_ID = UUID("00000000-0000-4000-8000-0000000000c5")
DYNAMIC_ID = UUID("00000000-0000-4000-8000-0000000000c6")
REUSED = ReusableRun(
    simulation_id=UUID("00000000-0000-4000-8000-0000000000d1"),
    simulation_run_id=UUID("00000000-0000-4000-8000-0000000000d2"),
)
CREATED_AT = datetime(2026, 11, 12, 15, tzinfo=UTC)
OUTPUT_BUCKET = "policyengine-v2-simulation-outputs-staging"
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
REFORM = {CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000}}
CATALOG = EconomyCatalogRows(
    catalog_version_id=CATALOG_VERSION_ID,
    model_id=MODEL_ID,
    region=CatalogRegion(id=REGION_ID, code="us", default_dataset_id=DATASET_ID),
    dataset=CatalogDataset(id=DATASET_ID, name="populace_us_2024"),
)
REPORT_BODY: dict[str, Any] = {
    "country": "us",
    "report_type": "economy",
    "label": "A larger child tax credit",
    "description": "Raises the credit to $3,000",
    "year": 2026,
    "region_code": "us",
    "dataset_name": None,
    "baseline": {"policy_id": None, "dynamic_id": None},
    "reform": {"policy_id": str(POLICY_ID), "dynamic_id": str(DYNAMIC_ID)},
    "include_cliffs": True,
    "spm": {"geography_kind": "national"},
}
SIMULATION_BODY: dict[str, Any] = {
    "country": "us",
    "simulation_type": "economy",
    "year": 2026,
    "region_code": "us",
    "dataset_name": None,
    "policy_id": str(POLICY_ID),
    "dynamic_id": None,
}


def report_request(**changes: Any) -> EconomyReportRequest:
    return EconomyReportRequest.model_validate({**REPORT_BODY, **changes})


def simulation_request(**changes: Any) -> EconomySimulationRequest:
    return EconomySimulationRequest.model_validate({**SIMULATION_BODY, **changes})


class FakeWorkers:
    """The default worker's economy functions, or the lookup's failure.

    ``worker_version`` is the manifest's default worker; without it, the
    economy fixtures' worker.
    """

    def __init__(
        self,
        failure: Exception | None = None,
        *,
        worker_version: V2WorkerVersion | None = None,
    ) -> None:
        self.failure = failure
        self.worker_version = worker_version

    def default_worker(self, country: str) -> EconomyWorker:
        if self.failure is not None:
            raise self.failure
        return default_economy_worker(economy_manifest(self.worker_version), country)


@dataclass
class FakeStore:
    """Records what the service asks of the database, and answers as told.

    ``claimed`` marks the run as claimed by its coordinator or function: a
    failure write then changes nothing.
    """

    catalog: EconomyCatalogRows = CATALOG
    reusable: tuple[ReusableRun | None, ReusableRun | None] = (None, None)
    read_failure: Exception | None = None
    create_failure: Exception | None = None
    record_failure: Exception | None = None
    fail_failure: Exception | None = None
    claimed: bool = False
    stored_report: EconomyReportState | None = None
    stored_simulation: EconomySimulationState | None = None
    # The catalog's local-area dataset a report read finds, when asked for one.
    local_area_dataset: CatalogDataset | None = None
    report_reads: list[tuple[EconomyRequest, Any]] = field(default_factory=list)
    # The local-area dataset each report read was asked for, or None.
    local_area_reads: list[str | None] = field(default_factory=list)
    simulation_reads: list[tuple[EconomyRequest, Any]] = field(default_factory=list)
    reports: list[NewEconomyReport] = field(default_factory=list)
    simulations: list[NewEconomySimulation] = field(default_factory=list)
    recorded_calls: list[tuple[UUID, str]] = field(default_factory=list)
    failed_runs: list[tuple[UUID, tuple[UUID, ...], str]] = field(default_factory=list)

    def read_report_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        reuse: RunReuse | None,
        local_area_dataset_name: str | None = None,
    ) -> EconomyReportInputs:
        self.report_reads.append((request, (baseline, reform, reuse)))
        self.local_area_reads.append(local_area_dataset_name)
        if self.read_failure is not None:
            raise self.read_failure
        baseline_reusable, reform_reusable = self.reusable
        inputs = EconomyReportInputs(
            catalog=self.catalog,
            baseline=SimulationInputs(
                choice=baseline, reform={}, reusable_run=baseline_reusable
            ),
            reform=SimulationInputs(
                choice=reform, reform=REFORM, reusable_run=reform_reusable
            ),
        )
        if local_area_dataset_name is None:
            return inputs
        return replace(inputs, local_area_dataset=self.local_area_dataset)

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        self.simulation_reads.append((request, choice))
        if self.read_failure is not None:
            raise self.read_failure
        return EconomySimulationInputs(
            catalog=self.catalog,
            simulation=SimulationInputs(choice=choice, reform=REFORM),
        )

    def read_report(self, report_id: UUID) -> EconomyReportState | None:
        if self.read_failure is not None:
            raise self.read_failure
        stored = self.stored_report
        return stored if stored is not None and stored.id == report_id else None

    def read_simulation(self, simulation_id: UUID) -> EconomySimulationState | None:
        if self.read_failure is not None:
            raise self.read_failure
        stored = self.stored_simulation
        return stored if stored is not None and stored.id == simulation_id else None

    def create_report(self, report: NewEconomyReport) -> None:
        if self.create_failure is not None:
            raise self.create_failure
        self.reports.append(report)

    def create_simulation(self, simulation: NewEconomySimulation) -> None:
        if self.create_failure is not None:
            raise self.create_failure
        self.simulations.append(simulation)

    def record_report_call(
        self, report_run_id: UUID, modal_call_id: str
    ) -> ReportRunStatus | None:
        if self.record_failure is not None:
            raise self.record_failure
        self.recorded_calls.append((report_run_id, modal_call_id))
        return ReportRunStatus.RUNNING if self.claimed else ReportRunStatus.PENDING

    def fail_unstarted_report(
        self,
        report_run_id: UUID,
        simulation_run_ids: Sequence[UUID],
        error_message: str,
    ) -> bool:
        if self.fail_failure is not None:
            raise self.fail_failure
        if self.claimed:
            return False
        self.failed_runs.append(
            (report_run_id, tuple(simulation_run_ids), error_message)
        )
        return True

    def record_simulation_call(
        self, simulation_run_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        if self.record_failure is not None:
            raise self.record_failure
        self.recorded_calls.append((simulation_run_id, modal_call_id))
        return SimulationStatus.RUNNING if self.claimed else SimulationStatus.PENDING

    def fail_unstarted_simulation(
        self, simulation_run_id: UUID, error_message: str
    ) -> bool:
        if self.fail_failure is not None:
            raise self.fail_failure
        if self.claimed:
            return False
        self.failed_runs.append((simulation_run_id, (), error_message))
        return True


@dataclass
class FakeCaller:
    """Starts calls by recording them; ``failure`` is raised instead when set.

    ``hang`` makes a start wait until the service stops waiting for it.
    """

    failure: Exception | None = None
    hang: bool = False
    report_runs: list[tuple[EconomyWorker, dict, dict, Any]] = field(
        default_factory=list
    )
    simulation_runs: list[tuple[EconomyWorker, dict, dict, Any]] = field(
        default_factory=list
    )

    async def start_report_run(
        self,
        worker: EconomyWorker,
        report_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        await self._maybe_fail()
        self.report_runs.append(
            (worker, report_payload, context_payload, observability_context)
        )
        return "fc-coordinator"

    async def start_simulation_run(
        self,
        worker: EconomyWorker,
        simulation_payload: dict[str, Any],
        context_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        await self._maybe_fail()
        self.simulation_runs.append(
            (worker, simulation_payload, context_payload, observability_context)
        )
        return "fc-worker"

    async def _maybe_fail(self) -> None:
        if self.hang:
            await asyncio.sleep(60)
        if self.failure is not None:
            raise self.failure


def numbered_ids():
    """Return a function giving 00…01, 00…02 and so on, in order."""
    numbers = count(1)
    return lambda: UUID(int=next(numbers))


def service(
    store: FakeStore,
    *,
    workers: FakeWorkers | None = None,
    caller: FakeCaller | None = None,
    output_bucket: str | None = OUTPUT_BUCKET,
) -> EconomySubmissionService:
    return EconomySubmissionService(
        store=store,
        workers=workers or FakeWorkers(),
        caller=caller or FakeCaller(),
        settings=ServiceSettings(
            environment="staging",
            modal_environment="staging",
            output_bucket=output_bucket,
        ),
        call_start_timeout_seconds=0.05,
        new_id=numbered_ids(),
        clock=lambda: CREATED_AT,
    )
