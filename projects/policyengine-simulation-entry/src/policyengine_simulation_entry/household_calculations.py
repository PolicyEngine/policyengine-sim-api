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

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from policyengine_simulation_persistence import NewSimulationRun, RunStatus
from sqlalchemy.exc import IntegrityError

from policyengine_simulation_entry.household_recording import policy_key_for_run
from policyengine_simulation_entry.modal_calls import ModalRequest

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from uuid import UUID

    from policyengine_simulation_contract.household_execution import (
        HouseholdDocument,
    )
    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        NewSimulation,
        SimulationRecord,
        StartedSimulationRun,
    )
    from pydantic import JsonValue

    from policyengine_simulation_entry.household_api_models import (
        HouseholdCalculationOptions,
        HouseholdCalculationSubmission,
    )
    from policyengine_simulation_entry.household_workers import HouseholdWorker
    from policyengine_simulation_entry.run_dispatch import DispatchOutcome

UNKNOWN_REFERENCE_MESSAGE = (
    "The household, policy or catalog version does not exist for this country."
)


class UnknownHouseholdReferenceError(LookupError):
    """The household, policy or catalog version does not exist for the country."""


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
    def country(self) -> Literal["us", "uk"]:
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
        started = await self._start(submission)
        run = started.run
        if run.status is RunStatus.PENDING:
            await self.dispatch.start_simulation_runs(
                [run.id],
                household_modal_request(
                    worker,
                    calculation_id=run.id,
                    inputs=submission,
                    run_policies=[(run.id, submission.policy.parameters)],
                ),
            )
        calculation = await self.read(run.id)
        if calculation is None:  # pragma: no cover - runs are never deleted
            msg = f"household calculation {run.id} disappeared"
            raise RuntimeError(msg)
        return calculation

    async def _start(
        self, submission: HouseholdCalculationSubmission
    ) -> StartedSimulationRun:
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

        try:
            return await asyncio.to_thread(start)
        except IntegrityError as error:
            raise UnknownHouseholdReferenceError(UNKNOWN_REFERENCE_MESSAGE) from error

    async def read(self, calculation_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation, or None if there is none."""
        return await asyncio.to_thread(self.store.read_calculation, calculation_id)
