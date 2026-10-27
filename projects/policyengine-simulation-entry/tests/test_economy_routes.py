"""The v2 economy report routes: submit, read, re-run, list runs, look up."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import FakeBackend, make_settings
from economy_fakes import CTC_REFORM, US_VERSION, FakeCatalog, FakeReports, FakeRuns
from economy_fakes import FakeSimulations as FakeSimulationStore
from fastapi.testclient import TestClient
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.economy_reports import EconomyReports
from policyengine_simulation_entry.economy_routes import (
    EconomyReportReads,
    EconomyRouteServices,
)
from policyengine_simulation_entry.economy_schemas import (
    EconomyErrorResponse,
    EconomyRefusalResponse,
)
from policyengine_simulation_entry.run_dispatch import RunDispatchService
from policyengine_simulation_persistence import RunErrorCode, RunStatus
from run_fakes import FakeDispatcher

RESULT = json.loads(
    (Path(__file__).parent / "fixtures" / "economy_result.json").read_text()
)
REFORM_POLICY = uuid4()
SUBMISSION = {
    "country": "us",
    "policyengine_version": "6.7.5",
    "year": 2026,
    "region_code": "state/ca",
    "baseline": {},
    "reform": {"policy_id": str(REFORM_POLICY)},
}


class _Results:
    """Return the stored result of a succeeded comparison."""

    def __init__(self) -> None:
        self.read: list[tuple] = []

    def read_result(self, report_run_id, catalog_version_id, country):
        self.read.append((report_run_id, catalog_version_id, country))
        return SingleYearMacroOutput.model_validate(RESULT)


class _Path:
    def __init__(self) -> None:
        self.catalog = FakeCatalog()
        self.catalog.changes[REFORM_POLICY] = CTC_REFORM
        self.simulations = FakeSimulationStore()
        self.simulations.policies_of_country["us"].add(REFORM_POLICY)
        self.reports = FakeReports()
        self.runs = FakeRuns()
        self.dispatcher = FakeDispatcher()
        self.results = _Results()
        self.backend = FakeBackend()
        services = EconomyRouteServices(
            reports=EconomyReports(
                simulations=self.simulations,
                reports=self.reports,
                runs=self.runs,
                catalog=self.catalog,
                transitions=self.runs,
                dispatch=RunDispatchService(
                    transitions=self.runs,
                    dispatcher=self.dispatcher,
                    clock=self.runs.clock,
                ),
                artifact_environment="test",
            ),
            reads=EconomyReportReads(
                simulations=self.simulations,
                reports=self.reports,
                runs=self.runs,
                catalog=self.catalog,
                results=self.results,
            ),
        )
        self.client = TestClient(
            create_app(
                settings=make_settings(),
                backend=self.backend,
                auth_dependency=lambda: None,
                run_services=RunServices(economy=services),
            )
        )

    def submit(self) -> dict:
        response = self.client.post("/v2/economy-reports", json=SUBMISSION)
        assert response.status_code == 201, response.text
        return response.json()


@pytest.fixture
def economy_path():
    path = _Path()
    with path.client:
        yield path


def test_a_submitted_report_is_running_its_simulations(economy_path) -> None:
    report = economy_path.submit()

    assert report["country"] == "us"
    assert report["year"] == 2026
    assert report["status"] == "running"
    assert report["result"] is None
    assert report["reform_simulation"]["policy_id"] == str(REFORM_POLICY)
    assert report["baseline_simulation"]["region_code"] == "state/ca"
    run = report["run"]
    assert run["trigger"] == "initial"
    assert run["policyengine_version"] == "6.7.5"
    assert run["catalog_version_id"] == str(US_VERSION.id)
    assert run["stage"] == "simulating"
    assert run["baseline_run"]["status"] == "running"
    assert run["comparison"]["status"] == "pending"
    assert economy_path.backend.requests == []


def test_a_report_is_read_back_with_its_result_once_compared(economy_path) -> None:
    report = economy_path.submit()
    runs = economy_path.runs
    report_run_id = report["run"]["id"]
    for side in ("baseline_run", "reform_run"):
        runs.succeed(runs.simulation_runs[_uuid(report["run"][side]["id"])].id)
    record = runs.report_runs_by_id[_uuid(report_run_id)]

    comparing = economy_path.client.get(f"/v2/economy-reports/{report['id']}")
    runs.report_runs_by_id[record.id] = replace(
        record, comparison_status=RunStatus.SUCCEEDED, comparison_modal_call_id="fc-c"
    )
    finished = economy_path.client.get(f"/v2/economy-reports/{report['id']}")

    assert comparing.json()["run"]["stage"] == "outputs"
    assert comparing.json()["result"] is None
    body = finished.json()
    assert body["status"] == "succeeded"
    assert body["run"]["stage"] is None
    assert body["result"] == SingleYearMacroOutput.model_validate(RESULT).model_dump(
        mode="json"
    )
    assert economy_path.results.read == [(record.id, US_VERSION.id, "us")]


def test_a_failed_simulation_run_fails_the_report_with_its_error(economy_path) -> None:
    report = economy_path.submit()
    runs = economy_path.runs
    reform_id = _uuid(report["run"]["reform_run"]["id"])
    runs.simulation_runs[reform_id] = replace(
        runs.simulation_runs[reform_id],
        status=RunStatus.FAILED,
        error_code=RunErrorCode.WORKER_ERROR.value,
        error_message="The Modal call raised ReportExecutionError.",
    )

    body = economy_path.client.get(f"/v2/economy-reports/{report['id']}").json()

    assert body["status"] == "failed"
    assert body["run"]["error"] == {
        "code": "worker_error",
        "message": "The Modal call raised ReportExecutionError.",
    }


def test_a_report_is_run_again_and_its_runs_are_listed_newest_first(
    economy_path,
) -> None:
    report = economy_path.submit()

    rerun = economy_path.client.post(
        f"/v2/economy-reports/{report['id']}/runs",
        json={"policyengine_version": "6.7.5", "trigger": "manual", "force": True},
    )
    listed = economy_path.client.get(f"/v2/economy-reports/{report['id']}/runs")

    assert rerun.status_code == 201
    assert rerun.json()["trigger"] == "manual"
    assert rerun.json()["baseline_run"]["attempt"] == 2
    assert [run["id"] for run in listed.json()["runs"]] == [
        rerun.json()["id"],
        report["run"]["id"],
    ]


def test_a_report_migrated_without_a_run_reads_as_not_run(economy_path) -> None:
    report = economy_path.submit()
    economy_path.runs.report_runs_by_id.clear()
    stored = economy_path.reports.rows[_uuid(report["id"])]
    economy_path.reports.rows[stored.id] = replace(stored, legacy_report_id=4321)

    by_id = economy_path.client.get(f"/v2/economy-reports/{report['id']}")
    by_legacy = economy_path.client.get("/v2/economy-reports/by-legacy-id/4321")

    assert by_id.json()["status"] == "not_run"
    assert by_id.json()["run"] is None
    assert by_legacy.json()["id"] == report["id"]
    assert by_legacy.json()["legacy_report_id"] == 4321


def test_a_simulation_is_read_by_id(economy_path) -> None:
    report = economy_path.submit()
    simulation_id = report["reform_simulation"]["id"]

    found = economy_path.client.get(f"/v2/simulations/{simulation_id}")
    missing = economy_path.client.get(f"/v2/simulations/{uuid4()}")

    assert found.status_code == 200
    assert found.json()["kind"] == "economy"
    assert found.json()["policy_id"] == str(REFORM_POLICY)
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        (
            "post",
            "/v2/economy-reports",
            {**SUBMISSION, "policyengine_version": "9.9.9"},
            422,
        ),
        ("post", "/v2/economy-reports", {**SUBMISSION, "options": {"spm": {}}}, 422),
        ("post", "/v2/economy-reports", {**SUBMISSION, "country": "ca"}, 422),
        ("get", f"/v2/economy-reports/{uuid4()}", None, 404),
        ("get", "/v2/economy-reports/by-legacy-id/99", None, 404),
        (
            "post",
            f"/v2/economy-reports/{uuid4()}/runs",
            {"policyengine_version": "6.7.5", "trigger": "manual"},
            404,
        ),
        ("get", f"/v2/economy-reports/{uuid4()}/runs", None, 404),
    ],
)
def test_a_request_sim_cannot_serve_is_refused(
    economy_path, method, path, body, status
) -> None:
    response = getattr(economy_path.client, method)(
        path, **({} if body is None else {"json": body})
    )

    assert response.status_code == status
    assert "detail" in response.json()


@pytest.mark.parametrize(
    ("route_request", "status", "detail_kind"),
    [
        (
            ("post", "/v2/economy-reports", {**SUBMISSION, "options": {"spm": {}}}),
            422,
            str,
        ),
        (("post", "/v2/economy-reports", {**SUBMISSION, "country": "ca"}), 422, list),
        (("get", "/v2/economy-reports/not-a-report-id", None), 422, list),
        (("get", f"/v2/economy-reports/{uuid4()}", None), 404, str),
        (("get", f"/v2/simulations/{uuid4()}", None), 404, str),
    ],
)
def test_each_error_answer_has_the_body_the_routes_declare(
    economy_path, route_request, status, detail_kind
) -> None:
    method, path, body = route_request
    response = getattr(economy_path.client, method)(
        path, **({} if body is None else {"json": body})
    )

    assert response.status_code == status
    declared = EconomyRefusalResponse if status == 422 else EconomyErrorResponse
    answer = declared.model_validate(response.json())
    assert isinstance(answer.detail, detail_kind)


def test_the_routes_answer_503_without_a_simulation_database() -> None:
    with TestClient(
        create_app(
            settings=make_settings(),
            backend=FakeBackend(),
            auth_dependency=lambda: None,
        )
    ) as client:
        response = client.post("/v2/economy-reports", json=SUBMISSION)

    assert response.status_code == 503
    assert isinstance(EconomyErrorResponse.model_validate(response.json()).detail, str)


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)


def test_a_household_report_is_not_an_economy_report(economy_path) -> None:
    report = economy_path.submit()
    stored = economy_path.reports.rows[_uuid(report["id"])]
    from policyengine_simulation_persistence import ReportKind

    economy_path.reports.rows[stored.id] = replace(stored, kind=ReportKind.HOUSEHOLD)

    read = economy_path.client.get(f"/v2/economy-reports/{report['id']}")
    runs = economy_path.client.get(f"/v2/economy-reports/{report['id']}/runs")

    assert read.status_code == 404
    assert runs.status_code == 404
