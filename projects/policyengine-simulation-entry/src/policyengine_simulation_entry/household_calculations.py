"""Submit household calculations, and read them as their Modal calls end.

A household calculation is one simulation run of a household simulation (a
household under one policy, with its options) for a catalog version and
year. Submitting one:

1. finds the household function the calculation's PolicyEngine.py version
   deployed, in the v2 version manifest, before anything is written;
2. reads, from the Public API's tables, the country's catalog version for
   that PolicyEngine.py version, the stored household (its document, saved
   SPM choice and ``default_year``, the year calculated) and each policy's
   parameter values by name, refusing a household, policy or catalog
   version the Public API does not have, a parameter the catalog version
   lacks, and a household without a default year or that the household
   functions cannot take, again before anything is written;
3. finds or creates the simulation, then reuses its run for that catalog
   version and year (while unfinished, or for 28 days after it succeeded,
   ``SUCCEEDED_RUN_REUSE_WINDOW``) or starts the next attempt;
4. starts the run's Modal call if the run is pending, whoever created it, so
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
one's call, and this submitter starts a call for the rest. Running a stored
report again reads its household and policies from the report's own
simulations.

This is v2 code: the v1 ``run_simulation`` code does not use it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_simulation_contract.household_execution import (
    HouseholdDocument,
    HouseholdExecutionRequest,
)
from policyengine_simulation_persistence import (
    NewReport,
    NewReportRun,
    NewSimulationRun,
    ReportKind,
    RunStatus,
)
from policyengine_simulation_persistence.reports import FIRST_YEAR, LAST_YEAR
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationOptions,
)
from policyengine_simulation_entry.household_conversion import (
    household_situation,
    saved_spm_choice,
)
from policyengine_simulation_entry.household_recording import policy_key_for_run
from policyengine_simulation_entry.modal_calls import ModalRequest
from policyengine_simulation_entry.run_dispatch import DispatchOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from uuid import UUID

    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        HouseholdCatalogVersion,
        HouseholdReport,
        NewSimulation,
        ReportRecord,
        ReportRunRecord,
        RunTrigger,
        SimulationRecord,
        SimulationRunRecord,
        StartedSimulationRun,
        StoredHousehold,
    )
    from pydantic import JsonValue

    from policyengine_simulation_entry.household_api_models import (
        HouseholdCalculationSubmission,
        HouseholdReportRunSubmission,
        HouseholdReportSubmission,
    )
    from policyengine_simulation_entry.household_workers import HouseholdWorker

UNKNOWN_REFERENCE_MESSAGE = (
    "The household, policy or catalog version does not exist for this country."
)


class UnknownHouseholdReferenceError(LookupError):
    """The database refused a household, policy or catalog version reference.

    The rows were read before writing, so the Public API deleted one since.
    """


class HouseholdNotCalculableError(ValueError):
    """The stored household has no default year, or the household functions refuse it.

    The message names the household and why.
    """


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


class HouseholdRowReader(Protocol):
    """Read the Public API's rows a household run needs (``HouseholdInputReader``)."""

    def catalog_version(
        self, country: str, policyengine_version: str
    ) -> HouseholdCatalogVersion:
        """Return the country's catalog version for a PolicyEngine.py version."""
        ...

    def household(self, country: str, household_id: UUID) -> StoredHousehold:
        """Return the country's stored household."""
        ...

    def policy_parameters(
        self, country: str, policy_id: UUID, catalog_version_id: UUID
    ) -> dict[str, dict[str, Any]]:
        """Return the values the country's policy sets, by name and period."""
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
class HouseholdRunInputs:
    """What a household's runs calculate, read from the Public API's rows.

    ``year`` is the household's ``default_year``; ``parameters`` holds each
    policy's values, by policy ID (None for current law, which changes
    nothing).
    """

    country: str
    catalog_version_id: UUID
    year: int
    household: HouseholdDocument
    spm: JsonValue
    spm_requested: bool
    options: HouseholdCalculationOptions
    parameters: Mapping[UUID | None, Mapping[str, object]]


def _calculable_household(country: str, stored: StoredHousehold) -> HouseholdDocument:
    """Return the stored household as the household functions take it.

    Raise HouseholdNotCalculableError when it has no default year in the
    runs' range, or the household functions' contract refuses it, such as a
    group no person belongs to.
    """
    year = stored.default_year
    if year is None or not FIRST_YEAR <= year <= LAST_YEAR:
        msg = f"the household {stored.id} has no default year to calculate"
        raise HouseholdNotCalculableError(msg)
    try:
        return HouseholdDocument.model_validate(
            household_situation(country, stored.document)
        )
    except ValidationError as error:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        msg = f"the household {stored.id} cannot be calculated: {reasons}"
        raise HouseholdNotCalculableError(msg) from None
    except (KeyError, TypeError) as error:
        msg = f"the household {stored.id} cannot be calculated: {error!r}"
        raise HouseholdNotCalculableError(msg) from None


def read_household_inputs(  # noqa: PLR0913  # one keyword per thing a run calculates
    reads: HouseholdRowReader,
    *,
    country: str,
    household_id: UUID,
    policy_ids: Sequence[UUID | None],
    policyengine_version: str,
    options: HouseholdCalculationOptions,
) -> HouseholdRunInputs:
    """Read and check everything the household's runs calculate.

    Raise the reads' errors for a catalog version, household or policy the
    Public API does not have or a parameter the version lacks, and
    HouseholdNotCalculableError for a household the runs cannot calculate.
    """
    version = reads.catalog_version(country, policyengine_version)
    stored = reads.household(country, household_id)
    household = _calculable_household(country, stored)
    spm = saved_spm_choice(stored.document)
    parameters: dict[UUID | None, Mapping[str, object]] = {}
    for policy_id in dict.fromkeys(policy_ids):
        parameters[policy_id] = (
            {}
            if policy_id is None
            else reads.policy_parameters(country, policy_id, version.id)
        )
    # Checked in _calculable_household.
    year = stored.default_year
    if year is None:  # pragma: no cover
        msg = "a calculable household has a default year"
        raise RuntimeError(msg)
    return HouseholdRunInputs(
        country=country,
        catalog_version_id=version.id,
        year=year,
        household=household,
        spm=spm,
        # A saved null selection is no choice, as the contract reads it.
        spm_requested=spm is not None,
        options=options,
        parameters=parameters,
    )


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
    reads: HouseholdRowReader

    async def submit(
        self, submission: HouseholdCalculationSubmission
    ) -> HouseholdCalculation:
        """Record the calculation, start its call if needed, and return it.

        Raise HouseholdFunctionUnavailableError or HouseholdWorkersUnavailableError
        and the row reads' refusals before anything is written, and
        UnknownHouseholdReferenceError when the database refuses a reference.
        """
        worker = await asyncio.to_thread(
            self.workers.resolve, submission.country, submission.policyengine_version
        )
        inputs = await asyncio.to_thread(
            lambda: read_household_inputs(
                self.reads,
                country=submission.country,
                household_id=submission.household_id,
                policy_ids=[submission.policy_id],
                policyengine_version=submission.policyengine_version,
                options=submission.options,
            )
        )

        def start() -> StartedSimulationRun:
            simulation = self.store.find_or_create_simulation(
                submission.simulation_definition()
            )
            return self.store.start_run(
                NewSimulationRun(
                    simulation_id=simulation.id,
                    tax_benefit_model_version_id=inputs.catalog_version_id,
                    year=inputs.year,
                    trigger=submission.trigger,
                ),
                force=submission.force,
            )

        run = (await self._write(start)).run
        await self._start_pending_runs(
            worker,
            calculation_id=run.id,
            inputs=inputs,
            runs=[(run, inputs.parameters[submission.policy_id])],
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

        The rows are read and the runs start before the report is created, so
        a submission sim cannot calculate leaves no report behind. Raise as
        ``submit`` does.
        """
        worker = await asyncio.to_thread(
            self.workers.resolve, submission.country, submission.policyengine_version
        )
        policy_ids = submission.policy_ids()
        inputs = await asyncio.to_thread(
            lambda: read_household_inputs(
                self.reads,
                country=submission.country,
                household_id=submission.household_id,
                policy_ids=policy_ids,
                policyengine_version=submission.policyengine_version,
                options=submission.options,
            )
        )

        def create() -> tuple[ReportRecord, list[SimulationRunRecord]]:
            simulations = [
                self.store.find_or_create_simulation(
                    submission.simulation_definition(policy_id)
                )
                for policy_id in policy_ids
            ]
            runs = self._start_side_runs(
                simulations,
                inputs=inputs,
                year=inputs.year,
                trigger=submission.trigger,
                force=submission.force,
            )
            report = self.store.create_report(
                NewReport(
                    kind=ReportKind.HOUSEHOLD,
                    country=submission.country,
                    baseline_simulation_id=simulations[0].id,
                    reform_simulation_id=(
                        simulations[1].id if len(simulations) > 1 else None
                    ),
                    year=inputs.year,
                )
            )
            return report, runs

        report, runs = await self._write(create)
        return await self._join_and_start(
            worker,
            report,
            inputs=inputs,
            sides=[
                (run, inputs.parameters[policy_id])
                for run, policy_id in zip(runs, policy_ids, strict=True)
            ],
            trigger=submission.trigger,
        )

    async def rerun_report(
        self, report_id: UUID, submission: HouseholdReportRunSubmission
    ) -> HouseholdReport | None:
        """Start another run of a stored household report; None if there is none.

        The household, policies and options are the report's simulations';
        the rows are read again for the chosen catalog version. Raise as
        ``submit`` does.
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
        household_id = simulations[0].household_id
        # The database requires a household simulation to name its household.
        if household_id is None:  # pragma: no cover
            msg = f"household report {report.id} has no household"
            raise RuntimeError(msg)
        policy_ids = [simulation.policy_id for simulation in simulations]
        worker = await asyncio.to_thread(
            self.workers.resolve, report.country, submission.policyengine_version
        )
        inputs = await asyncio.to_thread(
            lambda: read_household_inputs(
                self.reads,
                country=report.country,
                household_id=household_id,
                policy_ids=policy_ids,
                policyengine_version=submission.policyengine_version,
                options=HouseholdCalculationOptions.model_validate(
                    simulations[0].options
                ),
            )
        )
        runs = await self._write(
            lambda: self._start_side_runs(
                simulations,
                inputs=inputs,
                year=report.year,
                trigger=submission.trigger,
                force=submission.force,
            )
        )
        return await self._join_and_start(
            worker,
            report,
            inputs=inputs,
            sides=[
                (run, inputs.parameters[policy_id])
                for run, policy_id in zip(runs, policy_ids, strict=True)
            ],
            trigger=submission.trigger,
        )

    async def read_report(self, report_id: UUID) -> HouseholdReport | None:
        """Return a household report with its newest run, or None."""
        return await asyncio.to_thread(self.store.read_report, report_id)

    def _start_side_runs(  # one keyword per field of the runs
        self,
        simulations: Sequence[SimulationRecord],
        *,
        inputs: HouseholdRunInputs,
        year: int,
        trigger: RunTrigger,
        force: bool,
    ) -> list[SimulationRunRecord]:
        """Start or reuse a run of each side; a simulation on both sides runs once."""
        started: dict[UUID, SimulationRunRecord] = {}
        for simulation in simulations:
            if simulation.id not in started:
                started[simulation.id] = self.store.start_run(
                    NewSimulationRun(
                        simulation_id=simulation.id,
                        tax_benefit_model_version_id=inputs.catalog_version_id,
                        year=year,
                        trigger=trigger,
                    ),
                    force=force,
                ).run
        return [started[simulation.id] for simulation in simulations]

    async def _join_and_start(
        self,
        worker: HouseholdWorker,
        report: ReportRecord,
        *,
        inputs: HouseholdInputs,
        sides: Sequence[tuple[SimulationRunRecord, Mapping[str, object]]],
        trigger: RunTrigger,
    ) -> HouseholdReport:
        """Join the sides' runs in a report run, and start those still pending."""
        report_run = await self._write(
            lambda: self.store.create_report_run(
                NewReportRun(
                    report_id=report.id,
                    baseline_simulation_run_id=sides[0][0].id,
                    reform_simulation_run_id=(
                        sides[1][0].id if len(sides) > 1 else None
                    ),
                    trigger=trigger,
                )
            )
        )
        unique_runs = {run.id: (run, parameters) for run, parameters in sides}
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
