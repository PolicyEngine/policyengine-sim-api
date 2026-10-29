"""The v2 budget-window report routes, over in-memory stores."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from budget_window_fakes import REFORM_POLICY, WindowPath, finish_year
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_contract.call_reports import (
    CallEndedReport,
    CallReportStatus,
)
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.budget_window_reads import BudgetWindowReportReads
from policyengine_simulation_entry.budget_window_reports import (
    DEFAULT_MAX_CONCURRENT_YEARS,
    YearStartingRecorder,
)
from policyengine_simulation_entry.budget_window_routes import (
    BudgetWindowRouteServices,
)
from policyengine_simulation_entry.economy_routes import (
    EconomyReportReads,
    EconomyRouteServices,
)
from policyengine_simulation_entry.economy_schemas import (
    EconomyErrorResponse,
    EconomyRefusalResponse,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_persistence import RunStatus, YearBudget

SUBMISSION = {
    "country": "us",
    "policyengine_version": "6.7.5",
    "start_year": 2026,
    "window_size": 3,
    "region_code": "state/ca",
    "baseline": {},
    "reform": {"policy_id": str(REFORM_POLICY)},
}


class _RoutePath(WindowPath):
    def __init__(self, *, configured: bool = True) -> None:
        super().__init__(max_concurrent_years=2)
        self.services = BudgetWindowRouteServices(
            reports=self.budget_windows,
            reads=BudgetWindowReportReads(
                simulations=self.simulations,
                reports=self.reports,
                windows=self.windows,
                catalog=self.catalog,
            ),
        )
        economy = EconomyRouteServices(
            reports=self.economy,
            reads=EconomyReportReads(
                simulations=self.simulations,
                reports=self.reports,
                runs=self.runs,
                catalog=self.catalog,
                results=None,  # type: ignore[arg-type]
            ),
        )
        self.client = TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
                run_services=RunServices(
                    economy=economy,
                    budget_windows=self.services if configured else None,
                ),
            )
        )

    def submit(self, **changes: object) -> dict:
        response = self.client.post(
            "/v2/budget-window-reports", json={**SUBMISSION, **changes}
        )
        assert response.status_code == 201, response.text
        return response.json()

    def finish_started_years(self, run_id: UUID, budgetary_impact: float) -> None:
        for report_run in self.year_runs(run_id).values():
            if report_run.comparison_status is RunStatus.SUCCEEDED:
                continue
            finish_year(self.runs, report_run)
            self.windows.budgets[report_run.id] = YearBudget(
                year=report_run.year,
                budgetary_impact=budgetary_impact,
                tax_revenue_impact=1.0,
                state_tax_revenue_impact=0.0,
                benefit_spending_impact=2.0,
            )


@pytest.fixture
def window_path():
    path = _RoutePath()
    with path.client:
        yield path


def test_a_submitted_window_is_calculating_its_first_years(window_path) -> None:
    report = window_path.submit()

    assert report["country"] == "us"
    assert report["start_year"] == 2026
    assert report["window_size"] == 3
    assert report["status"] == "running"
    assert report["result"] is None
    assert report["reform_simulation"]["policy_id"] == str(REFORM_POLICY)
    run = report["run"]
    assert run["trigger"] == "initial"
    assert run["policyengine_version"] == "6.7.5"
    assert run["max_concurrent_years"] == 2
    assert [(year["year"], year["status"]) for year in run["years"]] == [
        (2026, "running"),
        (2027, "running"),
        (2028, "pending"),
    ]


def test_a_window_reads_back_its_result_once_every_year_succeeded(
    window_path,
) -> None:
    report = window_path.submit()
    run_id = UUID(report["run"]["id"])
    window_path.finish_started_years(run_id, -1.25)
    asyncio.run(
        window_path.budget_windows.start_years_after_call(
            simulation_run_ids=None,
            report_run_id=window_path.year_runs(run_id)[2026].id,
        )
    )
    window_path.finish_started_years(run_id, -1.25)

    body = window_path.client.get(f"/v2/budget-window-reports/{report['id']}").json()

    assert body["status"] == "succeeded"
    assert body["run"]["progress"] == 100
    assert body["result"]["kind"] == "budgetWindow"
    assert body["result"]["totals"]["budgetaryImpact"] == -3.75
    assert [impact["year"] for impact in body["result"]["annualImpacts"]] == [
        "2026",
        "2027",
        "2028",
    ]


def test_a_window_is_run_again_and_its_runs_are_listed(window_path) -> None:
    report = window_path.submit()

    rerun = window_path.client.post(
        f"/v2/budget-window-reports/{report['id']}/runs",
        json={"policyengine_version": "6.7.5", "trigger": "manual", "force": True},
    )
    listed = window_path.client.get(f"/v2/budget-window-reports/{report['id']}/runs")

    assert rerun.status_code == 201, rerun.text
    assert rerun.json()["force"] is True
    assert rerun.json()["trigger"] == "manual"
    assert [run["id"] for run in listed.json()["runs"]] == [
        rerun.json()["id"],
        report["run"]["id"],
    ]


def test_unknown_and_single_year_reports_are_not_found(window_path) -> None:
    single_year, _ = asyncio.run(window_path.economy.submit(window_path.submission()))

    for report_id in (uuid4(), single_year.id):
        for method, path, body in (
            ("GET", f"/v2/budget-window-reports/{report_id}", None),
            ("GET", f"/v2/budget-window-reports/{report_id}/runs", None),
            (
                "POST",
                f"/v2/budget-window-reports/{report_id}/runs",
                {"policyengine_version": "6.7.5", "trigger": "manual"},
            ),
        ):
            response = window_path.client.request(method, path, json=body)
            assert response.status_code == 404, (method, path)
            assert "budget-window report" in (
                EconomyErrorResponse.model_validate(response.json()).detail
            )


def test_a_window_sim_cannot_run_or_that_is_malformed_is_refused(
    window_path,
) -> None:
    unknown_region = window_path.client.post(
        "/v2/budget-window-reports", json={**SUBMISSION, "region_code": "state/zz"}
    )
    too_late = window_path.client.post(
        "/v2/budget-window-reports",
        json={**SUBMISSION, "start_year": 2095, "window_size": 10},
    )

    assert unknown_region.status_code == 422
    assert "state/zz" in str(
        EconomyRefusalResponse.model_validate(unknown_region.json()).detail
    )
    assert too_late.status_code == 422
    assert "2099" in str(EconomyRefusalResponse.model_validate(too_late.json()).detail)
    assert window_path.windows.rows == {}


def test_without_the_budget_window_path_every_route_answers_503() -> None:
    path = _RoutePath(configured=False)
    report_id = uuid4()

    with path.client:
        responses = [
            path.client.post("/v2/budget-window-reports", json=SUBMISSION),
            path.client.get(f"/v2/budget-window-reports/{report_id}"),
            path.client.post(
                f"/v2/budget-window-reports/{report_id}/runs",
                json={"policyengine_version": "6.7.5", "trigger": "manual"},
            ),
            path.client.get(f"/v2/budget-window-reports/{report_id}/runs"),
        ]

    assert {response.status_code for response in responses} == {503}


class _Recorder:
    def __init__(self, answer: CallReportStatus) -> None:
        self.answer = answer
        self.reports: list[CallEndedReport] = []

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        self.reports.append(report)
        return self.answer


def test_a_recorded_comparison_starts_its_windows_next_year() -> None:
    path = WindowPath(max_concurrent_years=1)
    _, run = path.submit_window(window_size=2)
    first_year = path.year_runs(run.id)[2026]
    finish_year(path.runs, first_year)
    inner = _Recorder(CallReportStatus.RECORDED)
    recorder = YearStartingRecorder(inner, path.budget_windows)
    report = CallEndedReport(modal_call_id="fc-comparison", report_run_id=first_year.id)

    answer = asyncio.run(recorder.record_reported_call(report))

    assert answer is CallReportStatus.RECORDED
    assert inner.reports == [report]
    assert sorted(path.year_runs(run.id)) == [2026, 2027]


def test_a_report_not_recorded_starts_nothing_and_a_failure_is_only_logged() -> None:
    path = WindowPath(max_concurrent_years=1)
    _, run = path.submit_window(window_size=2)
    first_year = path.year_runs(run.id)[2026]
    finish_year(path.runs, first_year)
    report = CallEndedReport(modal_call_id="fc-comparison", report_run_id=first_year.id)

    still_running = asyncio.run(
        YearStartingRecorder(
            _Recorder(CallReportStatus.STILL_RUNNING), path.budget_windows
        ).record_reported_call(report)
    )
    assert still_running is CallReportStatus.STILL_RUNNING
    assert sorted(path.year_runs(run.id)) == [2026]

    def lose_the_database(*_args, **_kwargs):
        msg = "the database dropped the connection"
        raise ConnectionError(msg)

    path.runs.get_report_run = lose_the_database  # type: ignore[method-assign]
    recorded = asyncio.run(
        YearStartingRecorder(
            _Recorder(CallReportStatus.RECORDED), path.budget_windows
        ).record_reported_call(report)
    )
    assert recorded is CallReportStatus.RECORDED
    assert sorted(path.year_runs(run.id)) == [2026]


def test_the_database_builds_the_budget_window_path_on_the_economy_path() -> None:
    database = SimulationDatabase.from_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )
    try:
        economy = database.economy_routes(
            artifact_environment="staging", modal_environment="main"
        )

        windows = database.budget_window_routes(economy)
    finally:
        database.dispose()

    assert windows.reports.economy is economy.reports
    assert windows.reports.max_concurrent_years == DEFAULT_MAX_CONCURRENT_YEARS
    assert windows.reads.windows is windows.reports.windows
