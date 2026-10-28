"""The household report routes: create a report, run it again, read it."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from household_fakes import (
    CATALOG_VERSION_ID,
    DEFAULT_YEAR,
    HOUSEHOLD_ID,
    POLICY_ID,
    POLICYENGINE_VERSION,
)
from household_harness import Harness
from household_support import calculated_household, succeeded_outcome
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.modal_calls import CallSucceeded

REPORTS_PATH = "/v2/household-reports"
REFORM = {"policy_id": str(POLICY_ID)}


def report_body(**overrides: object) -> dict:
    """Return the JSON body of a report of current law against the stored reform."""
    body: dict[str, object] = {
        "country": "us",
        "policyengine_version": POLICYENGINE_VERSION,
        "household_id": str(HOUSEHOLD_ID),
        "reform": REFORM,
    }
    body.update(overrides)
    return body


def rerun_body(**overrides: object) -> dict:
    """Return the JSON body of another run of a report."""
    body: dict[str, object] = {"policyengine_version": POLICYENGINE_VERSION}
    body.update(overrides)
    return body


@pytest.fixture
def harness() -> Harness:
    """Return a household calculation service over fakes."""
    return Harness()


@pytest.fixture
def report_client(harness: Harness):
    """Return a client of an app serving the harness's household reports."""
    app = create_app(
        settings=make_settings(),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
        run_services=RunServices(household_calculations=harness.service),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_a_created_report_is_accepted_with_its_first_run(report_client) -> None:
    response = report_client.post(REPORTS_PATH, json=report_body())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["country"] == "us"
    assert body["year"] == DEFAULT_YEAR
    assert body["household_id"] == str(HOUSEHOLD_ID)
    assert body["trigger"] == "initial"
    assert body["catalog_version_id"] == str(CATALOG_VERSION_ID)
    assert body["baseline"]["policy_id"] is None
    assert body["reform"]["policy_id"] == str(POLICY_ID)
    assert body["baseline"]["status"] == body["reform"]["status"] == "running"


def test_a_report_reads_back_both_households_once_its_call_ended(
    report_client, harness
) -> None:
    created = report_client.post(REPORTS_PATH, json=report_body()).json()
    run_ids = (
        UUID(created["baseline"]["calculation_id"]),
        UUID(created["reform"]["calculation_id"]),
    )
    call_id = harness.transitions.runs[run_ids[0]].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(succeeded_outcome(run_ids))
    harness.report_call_ended(*run_ids)

    response = report_client.get(f"{REPORTS_PATH}/{created['report_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["report_run_id"] == created["report_run_id"]
    assert body["baseline"]["result"]["household"] == calculated_household(1000.0)
    assert body["reform"]["result"]["household"] == calculated_household(2000.0)


def test_a_report_runs_again_on_request(report_client) -> None:
    created = report_client.post(REPORTS_PATH, json=report_body()).json()

    response = report_client.post(
        f"{REPORTS_PATH}/{created['report_id']}/runs", json=rerun_body(force=True)
    )

    assert response.status_code == 202
    body = response.json()
    assert body["trigger"] == "manual"
    assert body["report_run_id"] != created["report_run_id"]
    assert body["baseline"]["attempt"] == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"household_id": str(HOUSEHOLD_ID)},
        {"reform": REFORM},
        {"catalog_version_id": str(CATALOG_VERSION_ID)},
    ],
    ids=["household", "policies", "catalog-version"],
)
def test_a_run_names_only_its_version(report_client, overrides) -> None:
    created = report_client.post(REPORTS_PATH, json=report_body()).json()

    response = report_client.post(
        f"{REPORTS_PATH}/{created['report_id']}/runs", json=rerun_body(**overrides)
    )

    assert response.status_code == 422


def test_an_unknown_report_is_not_found(report_client) -> None:
    unknown = uuid4()

    assert report_client.get(f"{REPORTS_PATH}/{unknown}").status_code == 404
    assert (
        report_client.post(
            f"{REPORTS_PATH}/{unknown}/runs", json=rerun_body()
        ).status_code
        == 404
    )


def test_an_unknown_household_is_refused(report_client, harness) -> None:
    del harness.reads.households[("us", HOUSEHOLD_ID)]

    response = report_client.post(REPORTS_PATH, json=report_body())

    assert response.status_code == 400
    assert "has no household" in response.json()["detail"]


def test_without_household_calculations_the_report_routes_are_unavailable() -> None:
    app = create_app(
        settings=make_settings(),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
    )
    with TestClient(app) as test_client:
        created = test_client.post(REPORTS_PATH, json=report_body())
        rerun = test_client.post(f"{REPORTS_PATH}/{uuid4()}/runs", json=rerun_body())
        read = test_client.get(f"{REPORTS_PATH}/{uuid4()}")

    assert {created.status_code, rerun.status_code, read.status_code} == {503}


def test_a_report_whose_reform_value_the_contract_refuses_leaves_nothing(
    report_client, harness
) -> None:
    harness.reads.policies[("us", POLICY_ID)] = {
        "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": [[1]]}
    }

    response = report_client.post(REPORTS_PATH, json=report_body())

    assert response.status_code == 422
    assert harness.store.reports == {}
    assert harness.store.report_runs == []
    assert harness.store.simulations == {}
    assert harness.dispatcher.requests == []


def test_a_rerun_on_a_version_lacking_a_parameter_gets_422_and_writes_nothing(
    report_client, harness
) -> None:
    created = report_client.post(REPORTS_PATH, json=report_body()).json()
    report_runs_before = list(harness.store.report_runs)
    runs_before = dict(harness.store.setups)
    harness.reads.missing_parameters[POLICY_ID] = ("gov.irs.retired",)

    response = report_client.post(
        f"{REPORTS_PATH}/{created['report_id']}/runs", json=rerun_body(force=True)
    )

    assert response.status_code == 422
    assert "gov.irs.retired" in response.json()["detail"]
    assert harness.store.report_runs == report_runs_before
    assert harness.store.setups == runs_before
