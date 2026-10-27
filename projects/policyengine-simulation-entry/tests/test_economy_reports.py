"""Submitting and re-running v2 economy reports: runs, reuse, calls, comparisons."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from economy_fakes import (
    CTC_REFORM,
    LABOR_SUPPLY_REFORM,
    US_VERSION,
    FakeCatalog,
    FakeReports,
    FakeRuns,
    FakeSimulations,
)
from policyengine_simulation_contract.economy_runs import (
    EconomyComparisonCall,
    EconomySimulationCall,
)
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_entry.economy_report_types import (
    EconomyReportSubmission,
    EconomyRequestError,
    ReportNotFoundError,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_reports import EconomyReports
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_persistence import (
    PolicyParametersMissingError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
    SimulationKind,
)
from run_fakes import FakeDispatcher

REFORM_POLICY = uuid4()
LABOR_SUPPLY_POLICY = uuid4()
MISSING_POLICY = uuid4()
OTHER_POLICY = uuid4()


class _Path:
    """The economy path over in-memory stores, with every spawned request kept."""

    def __init__(self) -> None:
        self.catalog = FakeCatalog()
        self.catalog.changes.update(
            {
                REFORM_POLICY: CTC_REFORM,
                LABOR_SUPPLY_POLICY: LABOR_SUPPLY_REFORM,
                MISSING_POLICY: PolicyParametersMissingError(("gov.retired",)),
                OTHER_POLICY: {
                    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 2500}
                },
            }
        )
        self.simulations = FakeSimulations()
        self.simulations.policies_of_country["us"].update(
            {REFORM_POLICY, LABOR_SUPPLY_POLICY, MISSING_POLICY, OTHER_POLICY}
        )
        self.reports = FakeReports()
        self.runs = FakeRuns()
        self.dispatcher = FakeDispatcher()
        self.economy = EconomyReports(
            simulations=self.simulations,
            reports=self.reports,
            runs=self.runs,
            catalog=self.catalog,
            transitions=self.runs,
            dispatch=RunDispatchService(
                transitions=self.runs, dispatcher=self.dispatcher, clock=self.runs.clock
            ),
            artifact_environment="staging",
        )

    def submit(self, **changes: object):
        values: dict[str, object] = {
            "country": "us",
            "policyengine_version": "6.7.5",
            "year": 2026,
            "region_code": "state/ca",
            "baseline": SimulationChoice(),
            "reform": SimulationChoice(policy_id=REFORM_POLICY),
        }
        values.update(changes)
        return asyncio.run(
            self.economy.submit(EconomyReportSubmission(**values))  # type: ignore[arg-type]
        )

    def simulation_calls(self) -> list[EconomySimulationCall]:
        return [
            EconomySimulationCall.model_validate(request.arguments[0])
            for request in self.dispatcher.requests
            if request.target.function.startswith("run_economy_simulation")
        ]

    def comparison_calls(self) -> list[EconomyComparisonCall]:
        return [
            EconomyComparisonCall.model_validate(request.arguments[0])
            for request in self.dispatcher.requests
            if request.target.function == "compare_economy_simulations"
        ]


def test_a_submission_starts_one_call_per_simulation_run_of_its_report() -> None:
    path = _Path()

    report, report_run = path.submit()

    assert report.year == 2026
    assert report.options == {}
    assert report_run.trigger is RunTrigger.INITIAL
    assert report_run.tax_benefit_model_version_id == US_VERSION.id
    calls = {call.simulation_run_id: call for call in path.simulation_calls()}
    assert set(calls) == {
        report_run.baseline_simulation_run_id,
        report_run.reform_simulation_run_id,
    }
    reform = calls[report_run.reform_simulation_run_id]
    assert reform.policy == CTC_REFORM
    assert reform.population.region_code == "state/ca"
    assert reform.population.dataset_name == "populace_us_2024_acs_local"
    assert reform.population.row_filter is not None
    assert reform.population.row_filter.household_value() == 6
    assert reform.population.state_code == "CA"
    assert reform.labor_supply_response is False
    assert reform.artifact_environment == "staging"
    assert calls[report_run.baseline_simulation_run_id].policy == {}
    assert {request.target.application for request in path.dispatcher.requests} == {
        v2_application_name("6.7.5")
    }
    assert {path.runs.simulation_runs[run_id].status for run_id in calls} == {
        RunStatus.RUNNING
    }
    assert path.comparison_calls() == []


def test_a_us_national_run_sends_each_states_filter() -> None:
    path = _Path()

    path.submit(region_code="us")

    population = path.simulation_calls()[0].population
    assert population.row_filter is None
    assert population.state_filters is not None
    assert population.state_filters["state/ca"].household_value() == 6


def test_current_law_that_already_ran_is_reused_and_only_the_reform_starts() -> None:
    path = _Path()
    _, first = path.submit()
    path.runs.succeed(first.baseline_simulation_run_id)
    path.dispatcher.requests.clear()

    _, second = path.submit(reform=SimulationChoice(policy_id=OTHER_POLICY))

    assert second.baseline_simulation_run_id == first.baseline_simulation_run_id
    assert [call.simulation_run_id for call in path.simulation_calls()] == [
        second.reform_simulation_run_id
    ]


def test_a_report_whose_runs_both_succeeded_starts_its_comparison_at_once() -> None:
    path = _Path()
    _, first = path.submit()
    path.runs.succeed(first.baseline_simulation_run_id)
    path.runs.succeed(first.reform_simulation_run_id)
    path.dispatcher.requests.clear()

    _, second = path.submit()

    assert path.simulation_calls() == []
    # The first report run's comparison was waiting for the same two runs.
    comparisons = {call.report_run_id: call for call in path.comparison_calls()}
    assert set(comparisons) == {first.id, second.id}
    comparison = comparisons[second.id]
    assert comparison.baseline.simulation_run_id == first.baseline_simulation_run_id
    assert comparison.reform.policy == CTC_REFORM
    assert comparison.reform.output_uri.endswith("output.parquet")
    assert comparison.region_code == "state/ca"
    assert comparison.dataset_name == "populace_us_2024_acs_local"
    assert path.runs.report_runs_by_id[second.id].comparison_status is (
        RunStatus.RUNNING
    )


def test_a_run_compared_with_itself_is_one_run_and_one_call() -> None:
    path = _Path()

    _, report_run = path.submit(reform=SimulationChoice())

    assert report_run.baseline_simulation_run_id == report_run.reform_simulation_run_id
    assert len(path.simulation_calls()) == 1


def test_a_labour_supply_report_calculates_both_runs_again_with_its_columns() -> None:
    path = _Path()
    _, first = path.submit()
    path.runs.succeed(first.baseline_simulation_run_id)
    path.dispatcher.requests.clear()

    _, report_run = path.submit(reform=SimulationChoice(policy_id=LABOR_SUPPLY_POLICY))

    assert report_run.baseline_simulation_run_id != first.baseline_simulation_run_id
    calls = path.simulation_calls()
    assert len(calls) == 2
    assert all(call.labor_supply_response for call in calls)


def test_a_policy_its_catalog_version_cannot_run_fails_its_run_clearly() -> None:
    path = _Path()

    _, report_run = path.submit(reform=SimulationChoice(policy_id=MISSING_POLICY))

    run = path.runs.simulation_runs[report_run.reform_simulation_run_id]
    assert run.status is RunStatus.FAILED
    assert run.error_code == RunErrorCode.INVALID_INPUT.value
    assert "gov.retired" in (run.error_message or "")
    assert [call.simulation_run_id for call in path.simulation_calls()] == [
        report_run.baseline_simulation_run_id
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"policyengine_version": "6.8.0"}, "no catalog for PolicyEngine.py 6.8.0"),
        ({"region_code": "state/zz"}, "no region state/zz"),
        ({"dataset_name": "populace_us_2019"}, "no dataset populace_us_2019"),
        ({"options": {"include_cliffs": True}}, "take no options"),
        ({"reform": SimulationChoice(policy_id=uuid4())}, "policy or dynamic"),
    ],
)
def test_a_submission_that_cannot_run_is_refused_before_anything_starts(
    changes, message
) -> None:
    path = _Path()

    with pytest.raises(EconomyRequestError, match=message):
        path.submit(**changes)

    assert path.reports.rows == {}
    assert path.runs.simulation_runs == {}
    assert path.dispatcher.requests == []


def test_a_rerun_reuses_runs_unless_forced() -> None:
    path = _Path()
    report, first = path.submit()

    rerun = asyncio.run(
        path.economy.run_report(
            report.id,
            policyengine_version="6.7.5",
            trigger=RunTrigger.MANUAL,
            force=False,
        )
    )
    forced = asyncio.run(
        path.economy.run_report(
            report.id,
            policyengine_version="6.7.5",
            trigger=RunTrigger.MANUAL,
            force=True,
        )
    )

    assert rerun.trigger is RunTrigger.MANUAL
    assert rerun.baseline_simulation_run_id == first.baseline_simulation_run_id
    assert forced.baseline_simulation_run_id != first.baseline_simulation_run_id
    assert path.runs.simulation_runs[forced.baseline_simulation_run_id].attempt == 2
    assert path.runs.latest_report_run(report.id) == forced


def test_a_report_without_a_run_is_calculated_on_the_systems_trigger() -> None:
    path = _Path()
    report, _ = path.submit()

    report_run = asyncio.run(
        path.economy.run_report(
            report.id,
            policyengine_version="6.7.5",
            trigger=RunTrigger.SYSTEM,
            force=False,
        )
    )

    assert report_run.trigger is RunTrigger.SYSTEM


@pytest.mark.parametrize(
    ("report_exists", "trigger", "error"),
    [
        (False, RunTrigger.MANUAL, ReportNotFoundError),
        (True, RunTrigger.INITIAL, EconomyRequestError),
    ],
)
def test_a_rerun_needs_an_existing_report_and_a_rerun_trigger(
    report_exists, trigger, error
) -> None:
    path = _Path()
    report, _ = path.submit()

    with pytest.raises(error):
        asyncio.run(
            path.economy.run_report(
                report.id if report_exists else uuid4(),
                policyengine_version="6.7.5",
                trigger=trigger,
                force=False,
            )
        )


def test_a_finished_run_starts_the_comparisons_waiting_for_it() -> None:
    path = _Path()
    _, report_run = path.submit()
    path.runs.succeed(report_run.baseline_simulation_run_id)
    asyncio.run(
        path.economy.start_comparisons_after([report_run.baseline_simulation_run_id])
    )
    assert path.comparison_calls() == []

    path.runs.succeed(report_run.reform_simulation_run_id)
    asyncio.run(
        path.economy.start_comparisons_after([report_run.reform_simulation_run_id])
    )

    assert [call.report_run_id for call in path.comparison_calls()] == [report_run.id]


def test_a_comparison_whose_output_is_no_longer_offered_fails_clearly() -> None:
    path = _Path()
    _, report_run = path.submit()
    path.runs.succeed(report_run.baseline_simulation_run_id)
    path.runs.succeed(report_run.reform_simulation_run_id)
    del path.runs.files[(report_run.reform_simulation_run_id, "simulation_output")]

    asyncio.run(
        path.economy.start_comparisons_after([report_run.reform_simulation_run_id])
    )

    comparison = path.runs.report_runs_by_id[report_run.id]
    assert comparison.comparison_status is RunStatus.FAILED
    assert comparison.comparison_error_code == RunErrorCode.RESULT_EXPIRED.value
    assert path.comparison_calls() == []


def _age(path: _Path, run_id, minutes: int) -> None:
    run = path.runs.simulation_runs[run_id]
    path.runs.simulation_runs[run_id] = replace(
        run, created_at=run.created_at - timedelta(minutes=minutes)
    )


def test_the_safety_check_starts_comparisons_and_runs_left_waiting() -> None:
    path = _Path()
    _, compared = path.submit()
    path.runs.succeed(compared.baseline_simulation_run_id)
    path.runs.succeed(compared.reform_simulation_run_id)
    path.dispatcher.requests.clear()
    # A submitter that stopped after recording its runs left this one pending.
    _, stranded = path.submit(reform=SimulationChoice(policy_id=LABOR_SUPPLY_POLICY))
    reform_id = stranded.reform_simulation_run_id
    path.runs.simulation_runs[reform_id] = replace(
        path.runs.simulation_runs[reform_id],
        status=RunStatus.PENDING,
        modal_call_id=None,
        next_check_at=None,
    )
    _age(path, reform_id, 11)
    path.dispatcher.requests.clear()

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.comparisons_started == 1
    assert resumed.runs_started == 1
    assert [call.report_run_id for call in path.comparison_calls()] == [compared.id]
    (call,) = path.simulation_calls()
    assert call.simulation_run_id == reform_id
    assert call.labor_supply_response is True


def test_the_safety_check_leaves_a_run_its_submitter_is_still_starting() -> None:
    path = _Path()
    _, report_run = path.submit()
    run_id = report_run.reform_simulation_run_id
    path.runs.simulation_runs[run_id] = replace(
        path.runs.simulation_runs[run_id], status=RunStatus.PENDING
    )
    path.dispatcher.requests.clear()

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.runs_started == 0
    assert path.dispatcher.requests == []


def test_a_run_a_labour_supply_report_waits_for_keeps_its_columns() -> None:
    path = _Path()
    _, labour_supply = path.submit(
        reform=SimulationChoice(policy_id=LABOR_SUPPLY_POLICY)
    )
    baseline_id = labour_supply.baseline_simulation_run_id
    # Its submitter stopped before starting the current-law run's call.
    path.runs.simulation_runs[baseline_id] = replace(
        path.runs.simulation_runs[baseline_id],
        status=RunStatus.PENDING,
        modal_call_id=None,
        next_check_at=None,
    )
    path.dispatcher.requests.clear()

    _, ordinary = path.submit(reform=SimulationChoice(policy_id=OTHER_POLICY))

    assert ordinary.baseline_simulation_run_id == baseline_id
    calls = {call.simulation_run_id: call for call in path.simulation_calls()}
    assert calls[baseline_id].labor_supply_response is True
    assert calls[ordinary.reform_simulation_run_id].labor_supply_response is False


def _strand(path: _Path, run_id) -> None:
    path.runs.simulation_runs[run_id] = replace(
        path.runs.simulation_runs[run_id],
        status=RunStatus.PENDING,
        modal_call_id=None,
        next_check_at=None,
    )
    _age(path, run_id, 11)


def test_one_run_that_cannot_resume_does_not_stop_the_others() -> None:
    path = _Path()
    _, first = path.submit()
    _, second = path.submit(reform=SimulationChoice(policy_id=OTHER_POLICY))
    for run_id in (first.reform_simulation_run_id, second.reform_simulation_run_id):
        _strand(path, run_id)
    # The first reform's region has since left the catalog.
    first_reform = path.simulations.rows[
        path.runs.simulation_runs[first.reform_simulation_run_id].simulation_id
    ]
    path.simulations.rows[first_reform.id] = replace(
        first_reform, region_code="state/zz"
    )
    path.dispatcher.requests.clear()

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.runs_started == 1
    assert [call.simulation_run_id for call in path.simulation_calls()] == [
        second.reform_simulation_run_id
    ]
    unresolvable = path.runs.simulation_runs[first.reform_simulation_run_id]
    assert unresolvable.status is RunStatus.FAILED
    assert unresolvable.error_code == RunErrorCode.INVALID_INPUT.value


def test_a_pending_run_no_report_compares_is_failed_not_kept_waiting() -> None:
    path = _Path()
    _, report_run = path.submit()
    run_id = report_run.reform_simulation_run_id
    _strand(path, run_id)
    path.runs.report_runs_by_id.clear()
    path.dispatcher.requests.clear()

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.runs_started == 0
    run = path.runs.simulation_runs[run_id]
    assert run.status is RunStatus.FAILED
    assert run.error_code == RunErrorCode.DISPATCH_ABANDONED.value
    assert path.dispatcher.requests == []


def test_a_pending_household_run_is_left_to_the_household_path() -> None:
    path = _Path()
    _, report_run = path.submit()
    run_id = report_run.reform_simulation_run_id
    _strand(path, run_id)
    path.runs.simulation_runs[run_id] = replace(
        path.runs.simulation_runs[run_id], simulation_kind=SimulationKind.HOUSEHOLD
    )
    path.dispatcher.requests.clear()

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.runs_started == 0
    assert path.runs.simulation_runs[run_id].status is RunStatus.PENDING


def test_only_comparisons_whose_call_started_are_counted() -> None:
    path = _Path()
    _, report_run = path.submit()
    path.runs.succeed(report_run.baseline_simulation_run_id)
    path.runs.succeed(report_run.reform_simulation_run_id)
    del path.runs.files[(report_run.reform_simulation_run_id, "simulation_output")]

    resumed = asyncio.run(path.economy.resume_waiting_work(now=path.runs.clock()))

    assert resumed.comparisons_started == 0
    assert path.runs.report_runs_by_id[report_run.id].comparison_status is (
        RunStatus.FAILED
    )
