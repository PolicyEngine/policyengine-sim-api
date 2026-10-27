"""Submit household calculations, and read them as their Modal calls end.

A household calculation is one simulation run of a household simulation (a
household under one policy, with its options) for a catalog version and
year. Submitting one:

1. finds the household function the calculation's PolicyEngine.py version
   deployed, in the v2 version manifest, before anything is written;
2. finds or creates the simulation, then reuses its run for that catalog
   version and year (while unfinished, or once it succeeded) or starts the
   next attempt; the database refuses a household, policy or catalog version
   the Public API does not have;
3. starts the run's Modal call if the run is pending, whoever created it, so
   a run another submitter left pending runs too. Only the submitter whose
   claim moves the run to ``dispatching`` starts the call
   (``RunDispatchService``). The household function reports when the call
   ends, and sim records its outcome then; the safety check covers a call
   that never reports and a run left ``dispatching``.

Reading returns the run as it stands: status, error and, once it succeeded,
the household. Nothing waits for Modal.

A household report compares the household under a baseline and a reform.
Creating one, or running a stored one again, starts or reuses a run of each
of its simulations, joins them in a report run, and starts one Modal call
for those still pending, so a new report's two households are calculated
together. When another submitter claimed one of them first, it starts that
one's call, and this submitter starts a call for the rest.

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_persistence import (
    NewReport,
    NewReportRun,
    NewSimulationRun,
    ReportKind,
    RunStatus,
)
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationOptions,
)
from policyengine_simulation_entry.household_recording import policy_key_for_run
from policyengine_simulation_entry.modal_calls import ModalRequest
from policyengine_simulation_entry.run_dispatch import DispatchOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from uuid import UUID

    from policyengine_simulation_contract.household_execution import (
        HouseholdDocument,
    )
    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        HouseholdReport,
        NewSimulation,
        ReportRecord,
        ReportRunRecord,
        RunTrigger,
        SimulationRecord,
        SimulationRunRecord,
        StartedSimulationRun,
    )
    from pydantic import JsonValue

    from policyengine_simulation_entry.household_api_models import (
        HouseholdCalculationSubmission,
        HouseholdPolicyInput,
        HouseholdReportRunSubmission,
        HouseholdReportSubmission,
    )
    from policyengine_simulation_entry.household_workers import HouseholdWorker

    # Each of a report's simulations with the policy it calculates.
    type ReportSides = Sequence[tuple[SimulationRecord, HouseholdPolicyInput]]

UNKNOWN_REFERENCE_MESSAGE = (
    "The household, policy or catalog version does not exist for this country."
)


MISMATCHED_REPORT_MESSAGE = (
    "A report's run calculates the report's own household and policies."
)


class UnknownHouseholdReferenceError(LookupError):
    """The household, policy or catalog version does not exist for the country."""


class ReportPoliciesMismatchError(ValueError):
    """A report's run names another household or other policies than the report."""


class HouseholdCalculationRecords(Protocol):
    """The ``HouseholdCalculationStore`` reads and writes a submission needs."""

    def find_or_create_simulation(
        self, new_simulation: NewSimulation
    ) -> SimulationRecord:
        """Return the household simulation with these inputs, or create it."""
        ...

    def start_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Reuse the run for this setup, or create the next attempt."""
        ...

    def read_calculation(self, run_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation run ``run_id``, or None."""
        ...

    def get_simulation(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation with this ID, or None."""
        ...

    def create_report(self, new_report: NewReport) -> ReportRecord:
        """Insert one report and return it."""
        ...

    def get_report(self, report_id: UUID) -> ReportRecord | None:
        """Return the household report with this ID, or None."""
        ...

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Add a report run joining runs of the report's simulations."""
        ...

    def read_report(
        self, report_id: UUID, *, report_run_id: UUID | None = None
    ) -> HouseholdReport | None:
        """Return a household report with a run and its two calculations."""
        ...


class SimulationRunStarter(Protocol):
    """Start the one Modal call that computes some runs (``RunDispatchService``)."""

    async def start_simulation_runs(
        self, run_ids: Collection[UUID], request: ModalRequest
    ) -> DispatchOutcome:
        """Claim the pending runs and start their call, once."""
        ...


class HouseholdWorkerSource(Protocol):
    """Find a country's household function (``ManifestHouseholdWorkers``)."""

    def resolve(self, country: str, policyengine_version: str) -> HouseholdWorker:
        """Return the function and bundle, or raise why there is none."""
        ...


class HouseholdInputs(Protocol):
    """What a household's Modal call calculates, besides its policies."""

    @property
    def country(self) -> str:
        """The household's country."""
        ...

    @property
    def household(self) -> HouseholdDocument:
        """The household in the situation shape, without axes."""
        ...

    @property
    def spm(self) -> JsonValue:
        """The household's saved SPM choice, when ``spm_requested``."""
        ...

    @property
    def spm_requested(self) -> bool:
        """Whether the household has a saved SPM choice."""
        ...

    @property
    def options(self) -> HouseholdCalculationOptions:
        """The axes and whether missing variables are added."""
        ...


@dataclass(frozen=True)
class ReportRunInputs:
    """What a stored report's Modal call calculates, besides its policies."""

    country: str
    household: HouseholdDocument
    spm: JsonValue
    spm_requested: bool
    options: HouseholdCalculationOptions


def _check_report_sides(
    report: ReportRecord,
    sides: ReportSides,
    submission: HouseholdReportRunSubmission,
) -> None:
    """Refuse a run that names another household or other policies."""
    submitted = [submission.baseline] + (
        [] if submission.reform is None else [submission.reform]
    )
    if (
        len(submitted) != len(sides)
        or (report.reform_simulation_id is None) != (submission.reform is None)
        or any(
            simulation.household_id != submission.household_id
            or simulation.policy_id != policy.policy_id
            for (simulation, _), policy in zip(sides, submitted, strict=False)
        )
    ):
        raise ReportPoliciesMismatchError(MISMATCHED_REPORT_MESSAGE)


def household_modal_request(
    worker: HouseholdWorker,
    *,
    calculation_id: UUID,
    inputs: HouseholdInputs,
    run_policies: Sequence[tuple[UUID, Mapping[str, object]]],
) -> ModalRequest:
    """Return the household function call that computes each run's policy.

    ``run_policies`` pairs each run with its policy's parameter values; the
    request names each policy by its run, so the result maps back. The call's
    second argument lists the runs, so its last step can report them.
    """
    situation = inputs.household.to_situation()
    stored_options = inputs.options.stored()
    if "axes" in stored_options:
        situation["axes"] = stored_options["axes"]
    payload: dict[str, object] = {
        "calculation_id": calculation_id,
        "country": inputs.country,
        "household": situation,
        "policies": [
            {"policy_key": policy_key_for_run(run_id), "parameters": parameters}
            for run_id, parameters in run_policies
        ],
        "add_missing_variables": inputs.options.add_missing_variables,
        # The household's choice is the one it was saved with.
        "spm_selection_source": "saved_household",
        "bundle": worker.bundle,
    }
    if inputs.spm_requested:
        payload["spm"] = inputs.spm
    request = HouseholdExecutionRequest.model_validate(payload)
    return ModalRequest(
        target=worker.target,
        arguments=(
            request.model_dump(mode="json"),
            [str(run_id) for run_id, _ in run_policies],
        ),
    )


@dataclass
class HouseholdCalculationService:
    """Submit household calculations and read them."""

    store: HouseholdCalculationRecords
    dispatch: SimulationRunStarter
    workers: HouseholdWorkerSource

    async def submit(
        self, submission: HouseholdCalculationSubmission
    ) -> HouseholdCalculation:
        """Record the calculation, start its call if needed, and return it.

        Raise HouseholdFunctionUnavailableError or HouseholdWorkersUnavailableError
        before anything is written, and UnknownHouseholdReferenceError when
        the database refuses its references.
        """
        worker = await asyncio.to_thread(
            self.workers.resolve, submission.country, submission.policyengine_version
        )

        def start() -> StartedSimulationRun:
            simulation = self.store.find_or_create_simulation(
                submission.simulation_definition()
            )
            return self.store.start_run(
                NewSimulationRun(
                    simulation_id=simulation.id,
                    tax_benefit_model_version_id=submission.catalog_version_id,
                    year=submission.year,
                    trigger=submission.trigger,
                ),
                force=submission.force,
            )

        run = (await self._write(start)).run
        await self._start_pending_runs(
            worker,
            calculation_id=run.id,
            inputs=submission,
            runs=[(run, submission.policy.parameters)],
        )
        calculation = await self.read(run.id)
        if calculation is None:  # pragma: no cover - runs are never deleted
            msg = f"household calculation {run.id} disappeared"
            raise RuntimeError(msg)
        return calculation

    async def read(self, calculation_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation, or None if there is none."""
        return await asyncio.to_thread(self.store.read_calculation, calculation_id)

    async def submit_report(
        self, submission: HouseholdReportSubmission
    ) -> HouseholdReport:
        """Create a household report and start its first run.

        Raise as ``submit`` does.
        """
        worker = await asyncio.to_thread(
            self.workers.resolve, submission.country, submission.policyengine_version
        )
        policies = [submission.baseline] + (
            [] if submission.reform is None else [submission.reform]
        )

        def create() -> tuple[ReportRecord, list[SimulationRecord]]:
            simulations = [
                self.store.find_or_create_simulation(
                    submission.simulation_definition(policy)
                )
                for policy in policies
            ]
            report = self.store.create_report(
                NewReport(
                    kind=ReportKind.HOUSEHOLD,
                    country=submission.country,
                    baseline_simulation_id=simulations[0].id,
                    reform_simulation_id=(
                        simulations[1].id if len(simulations) > 1 else None
                    ),
                    year=submission.year,
                )
            )
            return report, simulations

        report, simulations = await self._write(create)
        return await self._run_report(
            worker,
            report,
            inputs=submission,
            sides=list(zip(simulations, policies, strict=True)),
            submission=submission,
        )

    async def rerun_report(
        self, report_id: UUID, submission: HouseholdReportRunSubmission
    ) -> HouseholdReport | None:
        """Start another run of a stored household report; None if there is none.

        Raise ReportPoliciesMismatchError when the submission names another
        household or other policies, and otherwise as ``submit`` does.
        """

        def load() -> tuple[ReportRecord, list[SimulationRecord]] | None:
            report = self.store.get_report(report_id)
            if report is None:
                return None
            simulation_ids = [report.baseline_simulation_id] + (
                []
                if report.reform_simulation_id is None
                else [report.reform_simulation_id]
            )
            simulations = [
                self.store.get_simulation(simulation_id)
                for simulation_id in simulation_ids
            ]
            return report, [
                simulation for simulation in simulations if simulation is not None
            ]

        loaded = await asyncio.to_thread(load)
        if loaded is None:
            return None
        report, simulations = loaded
        policies = [submission.baseline] + (
            [] if submission.reform is None else [submission.reform]
        )
        sides = list(zip(simulations, policies, strict=False))
        _check_report_sides(report, sides, submission)
        worker = await asyncio.to_thread(
            self.workers.resolve, report.country, submission.policyengine_version
        )
        inputs = ReportRunInputs(
            country=report.country,
            household=submission.household,
            spm=submission.spm,
            spm_requested=submission.spm_requested,
            options=HouseholdCalculationOptions.model_validate(simulations[0].options),
        )
        return await self._run_report(
            worker, report, inputs=inputs, sides=sides, submission=submission
        )

    async def read_report(self, report_id: UUID) -> HouseholdReport | None:
        """Return a household report with its newest run, or None."""
        return await asyncio.to_thread(self.store.read_report, report_id)

    async def _run_report(
        self,
        worker: HouseholdWorker,
        report: ReportRecord,
        *,
        inputs: HouseholdInputs,
        sides: ReportSides,
        submission: HouseholdReportSubmission | HouseholdReportRunSubmission,
    ) -> HouseholdReport:
        """Start or reuse a run of each side, join them, and start the pending."""
        trigger: RunTrigger = submission.trigger

        def start() -> tuple[list[SimulationRunRecord], ReportRunRecord]:
            # A report comparing a simulation with itself runs it once.
            started: dict[UUID, SimulationRunRecord] = {}
            for simulation, _ in sides:
                if simulation.id not in started:
                    started[simulation.id] = self.store.start_run(
                        NewSimulationRun(
                            simulation_id=simulation.id,
                            tax_benefit_model_version_id=submission.catalog_version_id,
                            year=report.year,
                            trigger=trigger,
                        ),
                        force=submission.force,
                    ).run
            runs = [started[simulation.id] for simulation, _ in sides]
            report_run = self.store.create_report_run(
                NewReportRun(
                    report_id=report.id,
                    baseline_simulation_run_id=runs[0].id,
                    reform_simulation_run_id=runs[1].id if len(runs) > 1 else None,
                    trigger=trigger,
                )
            )
            return runs, report_run

        runs, report_run = await self._write(start)
        unique_runs = {
            run.id: (run, policy.parameters)
            for run, (_, policy) in zip(runs, sides, strict=True)
        }
        await self._start_pending_runs(
            worker,
            calculation_id=report_run.id,
            inputs=inputs,
            runs=list(unique_runs.values()),
        )
        read = await asyncio.to_thread(
            lambda: self.store.read_report(report.id, report_run_id=report_run.id)
        )
        if read is None:  # pragma: no cover - reports are never deleted
            msg = f"household report {report.id} disappeared"
            raise RuntimeError(msg)
        return read

    async def _start_pending_runs(
        self,
        worker: HouseholdWorker,
        *,
        calculation_id: UUID,
        inputs: HouseholdInputs,
        runs: Sequence[tuple[SimulationRunRecord, Mapping[str, object]]],
    ) -> None:
        """Start one call for the runs that are pending, whoever created them.

        The claim moves every run or none, so when another submitter claimed
        one of them first, its call computes that one, and the others are
        claimed again without it.
        """
        pending = [
            (run.id, parameters)
            for run, parameters in runs
            if run.status is RunStatus.PENDING
        ]
        while pending:
            outcome = await self.dispatch.start_simulation_runs(
                [run_id for run_id, _ in pending],
                household_modal_request(
                    worker,
                    calculation_id=calculation_id,
                    inputs=inputs,
                    run_policies=pending,
                ),
            )
            if outcome is not DispatchOutcome.NOT_CLAIMED:
                return
            still_pending = []
            for run_id, parameters in pending:
                calculation = await self.read(run_id)
                if calculation is not None and calculation.run.status is (
                    RunStatus.PENDING
                ):
                    still_pending.append((run_id, parameters))
            # A claim fails only when a run is no longer pending.
            if len(still_pending) == len(pending):  # pragma: no cover
                return
            pending = still_pending

    async def _write[T](self, operation: Callable[[], T]) -> T:
        """Run database writes off the event loop, naming a refused reference."""
        try:
            return await asyncio.to_thread(operation)
        except IntegrityError as error:
            raise UnknownHouseholdReferenceError(UNKNOWN_REFERENCE_MESSAGE) from error
