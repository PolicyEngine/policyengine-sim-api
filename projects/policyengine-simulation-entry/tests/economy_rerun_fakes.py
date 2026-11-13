"""A fake of the economy store with re-runs, and stored records to re-run.

The store's own reads and writes are tested against PostgreSQL in the
persistence library; here a fake records what the re-run service asks of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID

from economy_service_fakes import (
    CREATED_AT,
    DYNAMIC_ID,
    OUTPUT_BUCKET,
    POLICY_ID,
    REFORM,
    FakeCaller,
    FakeWorkers,
    numbered_ids,
)
from policyengine_simulation_entry.economy_reruns import EconomyRerunService
from policyengine_simulation_entry.economy_submissions import ServiceSettings
from policyengine_simulation_persistence import (
    BudgetWindowInputs,
    EconomyReportInputs,
    EconomySimulationInputs,
    ReportDefinition,
    SideDefinition,
    SimulationChoice,
    SimulationDefinition,
    SimulationInputs,
    WindowYearDefinition,
)
from test_budget_window_submissions import FakeAdvancer, FakeWindowStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policyengine_simulation_entry.household_report_reruns import (
        HouseholdReportReruns,
    )
    from policyengine_simulation_persistence import (
        EconomyRequest,
        NewReportRerun,
        NewSimulationRerun,
        NewWindowRerun,
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
# A stored budget window of three years, each with its own simulations.
WINDOW_SIMULATION_IDS = {
    year: (
        UUID(f"00000000-0000-4000-8000-00000000{year}"),
        UUID(f"00000000-0000-4000-8000-00000001{year}"),
    )
    for year in (2026, 2027, 2028)
}
STORED_WINDOW = ReportDefinition(
    id=REPORT_ID,
    country="us",
    kind="budget_window",
    year=2026,
    region_code="us",
    dataset_name="populace_us_2024",
    inputs=STORED_OPTIONS,
    baseline=SideDefinition(
        simulation_id=WINDOW_SIMULATION_IDS[2026][0], choice=CURRENT_LAW
    ),
    reform=SideDefinition(
        simulation_id=WINDOW_SIMULATION_IDS[2026][1], choice=REFORM_CHOICE
    ),
    budget_window_size=3,
    window_years=tuple(
        WindowYearDefinition(
            year=year,
            baseline_simulation_id=baseline_id,
            reform_simulation_id=reform_id,
        )
        for year, (baseline_id, reform_id) in WINDOW_SIMULATION_IDS.items()
    ),
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
class FakeRerunStore(FakeWindowStore):
    """Records the re-run reads and writes, and answers as told.

    A budget window's re-run is read with ``reusable_by_year``, as a new
    window is, and recorded in ``windows``, where the fake advancer finds it.

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

    def read_window_rerun_inputs(
        self,
        request: EconomyRequest,
        *,
        baseline: SimulationChoice,
        reform: SimulationChoice,
        years: Sequence[WindowYearDefinition],
        reuse: RunReuse | None,
    ) -> BudgetWindowInputs:
        self.rerun_reads.append((request, (baseline, reform, tuple(years), reuse)))
        if self.inputs_failure is not None:
            raise self.inputs_failure
        return BudgetWindowInputs(
            catalog=self.catalog,
            baseline=SimulationInputs(choice=baseline, reform={}),
            reform=SimulationInputs(choice=reform, reform=REFORM),
            reusable_runs={
                year.year: (None, None)
                if reuse is None
                else self.reusable_by_year.get(year.year, (None, None))
                for year in years
            },
        )

    def create_window_rerun(self, rerun: NewWindowRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.windows.append(rerun)

    def create_report_rerun(self, rerun: NewReportRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.report_reruns.append(rerun)

    def create_simulation_rerun(self, rerun: NewSimulationRerun) -> None:
        if self.rerun_failure is not None:
            raise self.rerun_failure
        self.simulation_reruns.append(rerun)


class UnusedAdvancer:
    """A window advancer a test of a single-year re-run never expects to be used."""

    def advance_window(self, window_run_id: UUID, **_: Any) -> NoReturn:
        msg = f"a single-year re-run advanced window {window_run_id}"
        raise AssertionError(msg)


def rerun_service(  # noqa: PLR0913  # the store, then each seam a test may replace
    store: FakeRerunStore,
    *,
    workers: FakeWorkers | None = None,
    caller: FakeCaller | None = None,
    output_bucket: str | None = OUTPUT_BUCKET,
    advancer: FakeAdvancer | None = None,
    household_reports: HouseholdReportReruns | None = None,
) -> EconomyRerunService:
    return EconomyRerunService(
        household_reports=household_reports,
        windows=advancer or UnusedAdvancer(),
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
