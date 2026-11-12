"""A fake of the economy store with re-runs, and stored records to re-run.

The store's own reads and writes are tested against PostgreSQL in the
persistence library; here a fake records what the re-run service asks of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from economy_service_fakes import (
    CREATED_AT,
    DYNAMIC_ID,
    OUTPUT_BUCKET,
    POLICY_ID,
    REFORM,
    FakeCaller,
    FakeStore,
    FakeWorkers,
    numbered_ids,
)
from policyengine_simulation_entry.economy_reruns import EconomyRerunService
from policyengine_simulation_entry.economy_submissions import ServiceSettings
from policyengine_simulation_persistence import (
    EconomyReportInputs,
    EconomySimulationInputs,
    ReportDefinition,
    SideDefinition,
    SimulationChoice,
    SimulationDefinition,
    SimulationInputs,
)

if TYPE_CHECKING:
    from policyengine_simulation_persistence import (
        EconomyRequest,
        NewReportRerun,
        NewSimulationRerun,
        RunReuse,
    )

REPORT_ID = UUID("00000000-0000-4000-8000-0000000000a1")
BASELINE_SIMULATION_ID = UUID("00000000-0000-4000-8000-0000000000a2")
REFORM_SIMULATION_ID = UUID("00000000-0000-4000-8000-0000000000a3")
SIMULATION_ID = UUID("00000000-0000-4000-8000-0000000000a4")
RUN_UNDER_WAY_ID = UUID("00000000-0000-4000-8000-0000000000a5")
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
REFORM_CHOICE = SimulationChoice(policy_id=POLICY_ID, dynamic_id=DYNAMIC_ID)
STORED_OPTIONS: dict[str, Any] = {
    "include_cliffs": True,
    "spm": {"geography_kind": "national"},
}
STORED_REPORT = ReportDefinition(
    id=REPORT_ID,
    country="us",
    kind="economy",
    year=2026,
    region_code="us",
    dataset_name="populace_us_2024",
    inputs=STORED_OPTIONS,
    baseline=SideDefinition(simulation_id=BASELINE_SIMULATION_ID, choice=CURRENT_LAW),
    reform=SideDefinition(simulation_id=REFORM_SIMULATION_ID, choice=REFORM_CHOICE),
)
STORED_SIMULATION = SimulationDefinition(
    id=SIMULATION_ID,
    country="us",
    kind="economy",
    year=2027,
    region_code="us",
    dataset_name="populace_us_2024",
    choice=SimulationChoice(policy_id=POLICY_ID, dynamic_id=None),
)


@dataclass
class FakeRerunStore(FakeStore):
    """Records the re-run reads and writes, and answers as told.

    ``inputs_failure`` is raised by a re-run's read of its inputs, and
    ``rerun_failure`` by its create instead of recording it.
    """

    report_definition: ReportDefinition | None = STORED_REPORT
    simulation_definition: SimulationDefinition | None = STORED_SIMULATION
    inputs_failure: Exception | None = None
    rerun_failure: Exception | None = None
    rerun_reads: list[tuple[EconomyRequest, Any]] = field(default_factory=list)
    report_reruns: list[NewReportRerun] = field(default_factory=list)
    simulation_reruns: list[NewSimulationRerun] = field(default_factory=list)

    def read_report_definition(self, report_id: UUID) -> ReportDefinition | None:
        if self.read_failure is not None:
            raise self.read_failure
        stored = self.report_definition
        return stored if stored is not None and stored.id == report_id else None

    def read_simulation_definition(
        self, simulation_id: UUID
    ) -> SimulationDefinition | None:
        if self.read_failure is not None:
            raise self.read_failure
        stored = self.simulation_definition
        return stored if stored is not None and stored.id == simulation_id else None

    def read_report_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SideDefinition,
        reform: SideDefinition,
        reuse: RunReuse | None,
    ) -> EconomyReportInputs:
        self.rerun_reads.append((request, (baseline, reform, reuse)))
        if self.inputs_failure is not None:
            raise self.inputs_failure
        baseline_reusable, reform_reusable = self.reusable
        return EconomyReportInputs(
            catalog=self.catalog,
            baseline=SimulationInputs(
                choice=baseline.choice,
                reform={},
                reusable_run=None if reuse is None else baseline_reusable,
            ),
            reform=SimulationInputs(
                choice=reform.choice,
                reform=REFORM,
                reusable_run=None if reuse is None else reform_reusable,
            ),
        )

    def read_simulation_inputs(
        self, request: EconomyRequest, choice: SimulationChoice
    ) -> EconomySimulationInputs:
        if self.inputs_failure is not None:
            self.simulation_reads.append((request, choice))
            raise self.inputs_failure
        return super().read_simulation_inputs(request, choice)

    def create_report_rerun(self, rerun: NewReportRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.report_reruns.append(rerun)

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.simulation_reruns.append(rerun)


def rerun_service(
    store: FakeRerunStore,
    *,
    workers: FakeWorkers | None = None,
    caller: FakeCaller | None = None,
    output_bucket: str | None = OUTPUT_BUCKET,
) -> EconomyRerunService:
    return EconomyRerunService(
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
