"""Submitting household calculations, and reading them as their calls end."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from household_fakes import CATALOG_VERSION_ID, POLICYENGINE_VERSION
from household_harness import (
    CTC_REFORM,
    EARNINGS_AXES,
    HOUSEHOLD_ID,
    POLICY_ID,
    SPM_CHOICE,
    US_HOUSEHOLD,
    Harness,
    submission,
)
from household_support import calculated_household, succeeded_outcome
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_entry.household_calculations import (
    UnknownHouseholdReferenceError,
)
from policyengine_simulation_entry.household_recording import policy_key_for_run
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
)
from policyengine_simulation_entry.modal_calls import (
    CallFailed,
    CallSucceeded,
    ModalTarget,
)
from policyengine_simulation_persistence import (
    NewSimulationRun,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
)
from run_fakes import FakeDispatcher


def test_a_submitted_calculation_starts_one_household_call() -> None:
    harness = Harness()

    calculation = harness.submit(
        spm=SPM_CHOICE,
        policy={"policy_id": str(POLICY_ID), "parameters": CTC_REFORM},
        options={"axes": EARNINGS_AXES, "add_missing_variables": True},
    )

    assert calculation.run.status is RunStatus.RUNNING
    assert calculation.simulation.policy_id == POLICY_ID
    assert calculation.simulation.options == {
        "axes": EARNINGS_AXES,
        "add_missing_variables": True,
    }
    (spawned,) = harness.dispatcher.requests
    assert spawned.target == ModalTarget(
        application=v2_application_name(POLICYENGINE_VERSION),
        function="calculate_household_us",
    )
    assert spawned.arguments[1] == [str(calculation.run.id)]
    request = harness.sent_request()
    assert request.calculation_id == calculation.run.id
    assert [policy.policy_key for policy in request.policies] == [
        policy_key_for_run(calculation.run.id)
    ]
    assert request.policies[0].parameters == CTC_REFORM
    situation = request.household.to_situation()
    assert situation["people"] == US_HOUSEHOLD["people"]
    assert situation["axes"] == EARNINGS_AXES
    assert request.add_missing_variables
    assert request.spm_requested
    assert request.spm == SPM_CHOICE
    assert request.spm_selection_source == "saved_household"
    assert request.bundle.policyengine_version == POLICYENGINE_VERSION


def test_a_household_without_an_spm_choice_inherits_the_defaults() -> None:
    harness = Harness()

    calculation = harness.submit()

    assert "spm" not in harness.dispatcher.requests[0].arguments[0]
    assert not harness.sent_request().spm_requested
    assert calculation.simulation.options == {}
    assert calculation.simulation.policy_id is None


def test_submitting_again_reuses_the_running_calculation() -> None:
    harness = Harness()

    first = harness.submit()
    again = harness.submit()

    assert again.run.id == first.run.id
    assert len(harness.dispatcher.requests) == 1


def test_a_pending_calculation_left_by_another_submitter_is_dispatched() -> None:
    harness = Harness()
    simulation = harness.store.find_or_create_simulation(
        submission().simulation_definition()
    )
    left_pending = harness.store.start_run(
        NewSimulationRun(
            simulation_id=simulation.id,
            tax_benefit_model_version_id=CATALOG_VERSION_ID,
            year=2026,
            trigger=RunTrigger.INITIAL,
        )
    ).run

    calculation = harness.submit()

    assert calculation.run.id == left_pending.id
    assert calculation.run.status is RunStatus.RUNNING
    assert len(harness.dispatcher.requests) == 1


def test_an_unsupported_version_writes_nothing() -> None:
    harness = Harness()

    with pytest.raises(HouseholdFunctionUnavailableError):
        harness.submit(policyengine_version="9.9.9")

    assert harness.store.simulations == {}
    assert harness.dispatcher.requests == []


def test_an_unknown_household_is_refused_without_a_call() -> None:
    harness = Harness()
    harness.store.unknown_households.add(HOUSEHOLD_ID)

    with pytest.raises(UnknownHouseholdReferenceError):
        harness.submit()

    assert harness.dispatcher.requests == []


def test_a_refused_call_reads_back_failed() -> None:
    harness = Harness(FakeDispatcher(failure=RuntimeError("Modal refused")))

    calculation = harness.submit()

    assert calculation.run.status is RunStatus.FAILED
    assert calculation.run.error_code == "dispatch_failed"


def test_a_calculation_is_recorded_when_its_call_reports_success() -> None:
    harness = Harness()
    submitted = harness.submit()
    call_id = harness.transitions.runs[submitted.run.id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(
        succeeded_outcome([submitted.run.id])
    )

    harness.report_call_ended(submitted.run.id)

    calculation = harness.read(submitted.run.id)
    assert calculation.run.status is RunStatus.SUCCEEDED
    assert calculation.output.household == calculated_household(1000.0)


def test_a_succeeded_calculation_runs_again_only_when_forced() -> None:
    harness = Harness()
    submitted = harness.submit()
    call_id = harness.transitions.runs[submitted.run.id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(
        succeeded_outcome([submitted.run.id])
    )
    harness.report_call_ended(submitted.run.id)

    reused = harness.submit()
    forced = harness.submit(force=True)

    assert reused.run.id == submitted.run.id
    assert forced.run.attempt == 2
    assert forced.run.status is RunStatus.RUNNING
    assert len(harness.dispatcher.requests) == 2


def test_a_failed_modal_call_reads_back_failed() -> None:
    harness = Harness()
    submitted = harness.submit()
    call_id = harness.transitions.runs[submitted.run.id].modal_call_id
    harness.inspector.outcomes[call_id] = CallFailed(
        RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised MemoryError.")
    )

    harness.report_call_ended(submitted.run.id)

    calculation = harness.read(submitted.run.id)
    assert calculation.run.status is RunStatus.FAILED
    assert calculation.run.error_code == "worker_error"
    assert calculation.output is None


class StoppingDispatcher(FakeDispatcher):
    """Stop the instance after it claimed the run, before Modal answers."""

    async def spawn(self, request):
        """Record the request, then stop as a stopped instance does."""
        self.requests.append(request)
        raise asyncio.CancelledError


def test_a_calculation_left_dispatching_fails_and_runs_again() -> None:
    harness = Harness(StoppingDispatcher())
    with pytest.raises(asyncio.CancelledError):
        harness.submit()
    (stopped_run_id,) = harness.store.setups
    assert harness.transitions.runs[stopped_run_id].status is RunStatus.DISPATCHING

    harness.clock.advance(timedelta(minutes=11))
    asyncio.run(harness.recorder.record_due_outcomes())

    abandoned = harness.read(stopped_run_id)
    assert abandoned.run.status is RunStatus.FAILED
    assert abandoned.run.error_code == "dispatch_abandoned"
    harness.dispatcher = FakeDispatcher()
    harness.service.dispatch.dispatcher = harness.dispatcher
    resubmitted = harness.submit()
    assert resubmitted.run.id != stopped_run_id
    assert resubmitted.run.attempt == 2
    assert resubmitted.run.status is RunStatus.RUNNING
