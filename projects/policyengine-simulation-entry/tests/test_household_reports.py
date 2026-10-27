"""Household reports: a baseline and a reform run, computed by one Modal call."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from household_fakes import CATALOG_VERSION_ID, POLICYENGINE_VERSION
from household_harness import (
    CTC_REFORM,
    HOUSEHOLD_ID,
    POLICY_ID,
    US_HOUSEHOLD,
    Harness,
)
from household_support import calculated_household, failed_outcome, succeeded_outcome
from policyengine_simulation_entry.household_api_models import (
    HouseholdReportRunSubmission,
    HouseholdReportSubmission,
    household_report_status,
)
from policyengine_simulation_entry.household_calculations import (
    ReportPoliciesMismatchError,
)
from policyengine_simulation_entry.household_recording import policy_key_for_run
from policyengine_simulation_entry.modal_calls import CallSucceeded
from policyengine_simulation_persistence import RunStatus, RunTrigger

if TYPE_CHECKING:
    from policyengine_simulation_entry.run_dispatch import DispatchOutcome

REFORM = {"policy_id": str(POLICY_ID), "parameters": CTC_REFORM}


def report_submission(**overrides: object) -> HouseholdReportSubmission:
    """Return a 2026 US household report of current law against the CTC reform."""
    values: dict[str, object] = {
        "country": "us",
        "catalog_version_id": CATALOG_VERSION_ID,
        "policyengine_version": POLICYENGINE_VERSION,
        "year": 2026,
        "household_id": HOUSEHOLD_ID,
        "household": US_HOUSEHOLD,
        "reform": REFORM,
    }
    values.update(overrides)
    return HouseholdReportSubmission.model_validate(values)


def rerun_submission(**overrides: object) -> HouseholdReportRunSubmission:
    """Return another run of the report on the same catalog version."""
    values: dict[str, object] = {
        "catalog_version_id": CATALOG_VERSION_ID,
        "policyengine_version": POLICYENGINE_VERSION,
        "household_id": HOUSEHOLD_ID,
        "household": US_HOUSEHOLD,
        "reform": REFORM,
    }
    values.update(overrides)
    return HouseholdReportRunSubmission.model_validate(values)


def submit_report(harness: Harness, **overrides: object):
    """Submit a report and return it as it reads afterwards."""
    return asyncio.run(harness.service.submit_report(report_submission(**overrides)))


def rerun_report(harness: Harness, report_id: UUID, **overrides: object):
    """Run a stored report again and return it as it reads afterwards."""
    return asyncio.run(
        harness.service.rerun_report(report_id, rerun_submission(**overrides))
    )


def report_runs(report) -> tuple[UUID, UUID]:
    """Return a report's baseline and reform run IDs."""
    return report.baseline.run.id, report.reform.run.id


def test_one_call_computes_a_reports_baseline_and_reform() -> None:
    harness = Harness()

    report = submit_report(harness)

    baseline_run_id, reform_run_id = report_runs(report)
    assert report.report_run.reform_simulation_run_id == reform_run_id
    assert report.baseline.simulation.policy_id is None
    assert report.reform.simulation.policy_id == POLICY_ID
    (spawned,) = harness.dispatcher.requests
    assert spawned.arguments[1] == [str(baseline_run_id), str(reform_run_id)]
    request = harness.sent_request()
    assert request.calculation_id == report.report_run.id
    assert [(policy.policy_key, policy.parameters) for policy in request.policies] == [
        (policy_key_for_run(baseline_run_id), {}),
        (policy_key_for_run(reform_run_id), CTC_REFORM),
    ]
    assert {report.baseline.run.status, report.reform.run.status} == {RunStatus.RUNNING}
    assert report.baseline.run.modal_call_id == report.reform.run.modal_call_id


def test_a_baseline_only_report_calculates_one_policy() -> None:
    harness = Harness()

    report = submit_report(harness, reform=None)

    assert report.reform is None
    assert report.report.reform_simulation_id is None
    assert len(harness.sent_request().policies) == 1


def test_the_shared_call_records_each_runs_household() -> None:
    harness = Harness()
    report = submit_report(harness)
    run_ids = report_runs(report)
    call_id = report.baseline.run.modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(succeeded_outcome(run_ids))

    harness.report_call_ended(*run_ids)

    read = asyncio.run(harness.service.read_report(report.report.id))
    assert read.baseline.output.household == calculated_household(1000.0)
    assert read.reform.output.household == calculated_household(2000.0)
    assert household_report_status(read) is RunStatus.SUCCEEDED


def test_a_rejected_shared_call_fails_the_report() -> None:
    harness = Harness()
    report = submit_report(harness)
    call_id = report.baseline.run.modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(failed_outcome())

    harness.report_call_ended(*report_runs(report))

    read = asyncio.run(harness.service.read_report(report.report.id))
    assert household_report_status(read) is RunStatus.FAILED
    assert read.reform.run.error_detail == failed_outcome()["error"]


def test_a_baseline_another_calculation_computed_is_reused() -> None:
    harness = Harness()
    baseline = harness.submit()
    call_id = harness.transitions.runs[baseline.run.id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(
        succeeded_outcome([baseline.run.id])
    )
    harness.report_call_ended(baseline.run.id)

    report = submit_report(harness)

    assert report.baseline.run.id == baseline.run.id
    assert report.baseline.run.status is RunStatus.SUCCEEDED
    request = harness.sent_request()
    assert [policy.policy_key for policy in request.policies] == [
        policy_key_for_run(report.reform.run.id)
    ]
    assert household_report_status(report) is RunStatus.RUNNING


class ClaimingFirstStarter:
    """Another submitter claims the baseline just before this one claims both."""

    def __init__(self, harness: Harness) -> None:
        """Wrap the harness's dispatch service."""
        self.dispatch = harness.service.dispatch
        self.harness = harness
        self.raced = False

    async def start_simulation_runs(self, run_ids, request) -> DispatchOutcome:
        """Let the other submitter claim the first run once, then start these."""
        if not self.raced:
            self.raced = True
            self.harness.transitions.claim_simulation_dispatch([next(iter(run_ids))])
        return await self.dispatch.start_simulation_runs(run_ids, request)


def test_runs_another_submitter_claimed_are_left_to_it() -> None:
    harness = Harness()
    harness.service.dispatch = ClaimingFirstStarter(harness)

    report = submit_report(harness)

    assert report.baseline.run.status is RunStatus.DISPATCHING
    assert report.reform.run.status is RunStatus.RUNNING
    (spawned,) = harness.dispatcher.requests
    assert spawned.arguments[1] == [str(report.reform.run.id)]


def test_a_report_runs_again_reusing_its_runs_unless_forced() -> None:
    harness = Harness()
    report = submit_report(harness)

    again = rerun_report(harness, report.report.id)
    forced = rerun_report(harness, report.report.id, force=True)

    assert report_runs(again) == report_runs(report)
    assert again.report_run.trigger is RunTrigger.MANUAL
    assert again.report_run.id != report.report_run.id
    assert forced.baseline.run.attempt == 2
    assert forced.reform.run.attempt == 2
    assert len(harness.dispatcher.requests) == 2
    latest = asyncio.run(harness.service.read_report(report.report.id))
    assert latest.report_run.id == forced.report_run.id


@pytest.mark.parametrize(
    "overrides",
    [
        {"reform": None},
        {"reform": {"policy_id": str(uuid4()), "parameters": {}}},
        {"baseline": {"policy_id": str(POLICY_ID), "parameters": {}}},
        {"household_id": str(uuid4())},
    ],
    ids=["no-reform", "other-reform", "other-baseline", "other-household"],
)
def test_a_run_must_calculate_the_reports_own_policies(overrides) -> None:
    harness = Harness()
    report = submit_report(harness)

    with pytest.raises(ReportPoliciesMismatchError):
        rerun_report(harness, report.report.id, **overrides)

    assert len(harness.dispatcher.requests) == 1


def test_an_unknown_report_has_no_runs_to_start() -> None:
    harness = Harness()

    assert rerun_report(harness, uuid4()) is None
    assert asyncio.run(harness.service.read_report(uuid4())) is None
