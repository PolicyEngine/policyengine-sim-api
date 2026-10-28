"""Submitting household calculations, and reading them as their calls end.

A submission names the stored household, the policy and the PolicyEngine.py
version; sim reads the household, the policy's values and the catalog
version itself, and calculates the household's ``default_year``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from household_fakes import (
    CATALOG_VERSION_ID,
    CTC_REFORM,
    DEFAULT_YEAR,
    HOUSEHOLD_ID,
    POLICY_ID,
    POLICYENGINE_VERSION,
    SPM_CHOICE,
    US_SITUATION,
)
from household_harness import EARNINGS_AXES, Harness, submission
from household_support import calculated_household, succeeded_outcome
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_entry.household_calculations import (
    HouseholdNotCalculableError,
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
    SUCCEEDED_RUN_REUSE_WINDOW,
    NewSimulationRun,
    PolicyParametersMissingError,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
    UnknownCatalogVersionError,
    UnknownHouseholdError,
    UnknownPolicyError,
)
from run_fakes import FakeDispatcher
from sqlalchemy.exc import OperationalError


def test_a_submitted_calculation_starts_one_household_call() -> None:
    harness = Harness()
    harness.reads.store_household(DEFAULT_YEAR, spm=SPM_CHOICE)

    calculation = harness.submit(
        policy_id=str(POLICY_ID),
        options={"axes": EARNINGS_AXES, "add_missing_variables": True},
    )

    assert calculation.run.status is RunStatus.RUNNING
    assert calculation.run.tax_benefit_model_version_id == CATALOG_VERSION_ID
    assert calculation.run.year == DEFAULT_YEAR
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
    assert {key: value for key, value in situation.items() if key != "axes"} == (
        US_SITUATION
    )
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


def test_a_household_deleted_while_submitting_is_refused_without_a_call() -> None:
    harness = Harness()
    harness.store.unknown_households.add(HOUSEHOLD_ID)

    with pytest.raises(UnknownHouseholdReferenceError):
        harness.submit()

    assert harness.dispatcher.requests == []


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"household_id": "00000000-0000-4000-8000-00000000dead"},
            UnknownHouseholdError,
        ),
        ({"policy_id": "00000000-0000-4000-8000-00000000dead"}, UnknownPolicyError),
        ({"country": "uk"}, UnknownHouseholdError),
    ],
    ids=["household", "policy", "another-country"],
)
def test_a_household_or_policy_the_public_api_lacks_writes_nothing(
    overrides, error
) -> None:
    harness = Harness()

    with pytest.raises(error):
        harness.submit(**overrides)

    assert harness.store.simulations == {}
    assert harness.dispatcher.requests == []


def test_a_version_without_a_catalog_version_writes_nothing() -> None:
    harness = Harness()
    del harness.reads.catalog_versions[("us", POLICYENGINE_VERSION)]

    with pytest.raises(UnknownCatalogVersionError, match=POLICYENGINE_VERSION):
        harness.submit()

    assert harness.store.simulations == {}


def test_a_parameter_the_catalog_version_lacks_writes_nothing() -> None:
    harness = Harness()
    harness.reads.missing_parameters[POLICY_ID] = (
        "gov.irs.credits.retired_credit.amount",
    )

    with pytest.raises(PolicyParametersMissingError, match="retired_credit"):
        harness.submit(policy_id=str(POLICY_ID))

    assert harness.store.simulations == {}
    assert harness.dispatcher.requests == []


def test_a_household_without_a_default_year_is_not_calculable() -> None:
    harness = Harness()
    harness.reads.store_household(None)

    with pytest.raises(HouseholdNotCalculableError, match="default year"):
        harness.submit()

    assert harness.store.simulations == {}


def test_a_stored_household_the_contract_refuses_is_not_calculable() -> None:
    harness = Harness()
    harness.reads.store_household(
        DEFAULT_YEAR,
        family=[{"id": "your family", "values": {}}, {"id": "empty", "values": {}}],
    )

    with pytest.raises(HouseholdNotCalculableError, match="empty"):
        harness.submit()

    assert harness.store.simulations == {}


def test_an_unreadable_public_api_row_is_reported_as_unavailable() -> None:
    harness = Harness()
    harness.reads.error = OperationalError("SELECT", {}, ConnectionError("down"))

    with pytest.raises(OperationalError):
        harness.submit()

    assert harness.store.simulations == {}


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


def test_a_saved_null_spm_choice_is_no_choice() -> None:
    """A stored household's null selection inherits the defaults, as omission does."""
    harness = Harness()
    harness.reads.store_household(DEFAULT_YEAR, spm=None)

    harness.submit()

    assert "spm" not in harness.dispatcher.requests[0].arguments[0]
    assert not harness.sent_request().spm_requested


def test_a_calculation_that_succeeded_too_long_ago_runs_again() -> None:
    """Runs are reused only while their output files are kept."""
    harness = Harness()
    submitted = harness.submit()
    call_id = harness.transitions.runs[submitted.run.id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(
        succeeded_outcome([submitted.run.id])
    )
    harness.report_call_ended(submitted.run.id)

    harness.clock.advance(SUCCEEDED_RUN_REUSE_WINDOW + timedelta(days=1))
    again = harness.submit()

    assert again.run.id != submitted.run.id
    assert again.run.attempt == 2


def test_a_household_is_calculated_for_its_default_year_not_the_current_one() -> None:
    harness = Harness()
    harness.reads.store_household(2023)

    calculation = harness.submit(options={"add_missing_variables": True})

    assert calculation.run.year == 2023
    request = harness.sent_request()
    assert request.year == "2023"
    assert request.add_missing_variables
