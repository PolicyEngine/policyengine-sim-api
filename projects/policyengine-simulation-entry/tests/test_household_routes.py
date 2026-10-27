"""The household calculation routes: submit, then poll until the call ends."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from household_fakes import CATALOG_VERSION_ID, POLICYENGINE_VERSION
from household_harness import (
    EARNINGS_AXES,
    HOUSEHOLD_ID,
    POLICY_ID,
    US_HOUSEHOLD,
    Harness,
)
from household_support import (
    BUNDLE,
    SPM_CONFIG,
    calculated_household,
    failed_outcome,
    spm_receipt,
    succeeded_outcome,
)
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationService,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdWorkersUnavailableError,
    ManifestHouseholdWorkers,
)
from policyengine_simulation_entry.modal_calls import CallSucceeded
from policyengine_simulation_entry.simulation_database import SimulationDatabase

SUBMIT_PATH = "/v2/household-calculations"


def submission_body(**overrides: object) -> dict:
    """Return the JSON body of a 2026 current-law US calculation."""
    body: dict[str, object] = {
        "country": "us",
        "catalog_version_id": str(CATALOG_VERSION_ID),
        "policyengine_version": POLICYENGINE_VERSION,
        "year": 2026,
        "household_id": str(HOUSEHOLD_ID),
        "household": US_HOUSEHOLD,
    }
    body.update(overrides)
    return body


@pytest.fixture
def harness() -> Harness:
    """Return a household calculation service over fakes."""
    return Harness()


@pytest.fixture
def household_client(harness: Harness):
    """Return a client of an app serving the harness's household calculations."""
    app = create_app(
        settings=make_settings(),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
        run_services=RunServices(household_calculations=harness.service),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_a_submitted_calculation_is_accepted_and_running(household_client) -> None:
    response = household_client.post(SUBMIT_PATH, json=submission_body())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["household_id"] == str(HOUSEHOLD_ID)
    assert body["policy_id"] is None
    assert body["options"] == {}
    assert body["catalog_version_id"] == str(CATALOG_VERSION_ID)
    assert body["year"] == 2026
    assert body["attempt"] == 1
    assert body["trigger"] == "initial"
    assert body["result"] is None
    assert body["error"] is None


def test_a_succeeded_calculation_reads_back_its_household(
    household_client, harness
) -> None:
    submitted = household_client.post(
        SUBMIT_PATH,
        json=submission_body(
            policy={"policy_id": str(POLICY_ID), "parameters": {}},
            options={"axes": EARNINGS_AXES},
        ),
    ).json()
    run_id = submitted["calculation_id"]
    stored_id = UUID(run_id)
    call_id = harness.transitions.runs[stored_id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(succeeded_outcome([stored_id]))
    harness.report_call_ended(stored_id)

    response = household_client.get(f"{SUBMIT_PATH}/{run_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["policy_id"] == str(POLICY_ID)
    assert body["options"] == {"axes": EARNINGS_AXES}
    assert body["result"] == {
        "household": calculated_household(1000.0),
        "warnings": ["The input `age_group` is deprecated and was ignored."],
        "spm_config": SPM_CONFIG,
        "spm_provenance": spm_receipt(),
        "bundle": BUNDLE,
    }
    assert list(body["result"]["household"]) == ["households", "people"]


def test_a_rejected_calculation_reads_back_its_household_error(
    household_client, harness
) -> None:
    run_id = household_client.post(SUBMIT_PATH, json=submission_body()).json()[
        "calculation_id"
    ]
    stored_id = UUID(run_id)
    call_id = harness.transitions.runs[stored_id].modal_call_id
    harness.inspector.outcomes[call_id] = CallSucceeded(failed_outcome())
    harness.report_call_ended(stored_id)

    body = household_client.get(f"{SUBMIT_PATH}/{run_id}").json()

    assert body["status"] == "failed"
    assert body["result"] is None
    assert body["error"] == {
        "code": "household_calculation_failed",
        "message": "The household calculation failed: invalid_inputs.",
        "detail": failed_outcome()["error"],
    }


def test_an_unknown_calculation_is_not_found(household_client) -> None:
    response = household_client.get(f"{SUBMIT_PATH}/{uuid4()}")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"household": {**US_HOUSEHOLD, "axes": EARNINGS_AXES}},
        {"policy": {"parameters": {"gov.irs.credits.ctc.refundable": {}}}},
        {"year": 1899},
        {"country": "ca"},
    ],
    ids=["axes-in-household", "parameters-without-policy", "year", "country"],
)
def test_a_malformed_submission_is_refused_before_anything_runs(
    household_client, harness, overrides
) -> None:
    response = household_client.post(SUBMIT_PATH, json=submission_body(**overrides))

    assert response.status_code == 422
    assert harness.dispatcher.requests == []


def test_a_version_without_a_household_function_is_refused(household_client) -> None:
    response = household_client.post(
        SUBMIT_PATH, json=submission_body(policyengine_version="9.9.9")
    )

    assert response.status_code == 400
    assert "9.9.9" in response.json()["detail"]


def test_an_unknown_household_is_refused(household_client, harness) -> None:
    harness.store.unknown_households.add(HOUSEHOLD_ID)

    response = household_client.post(SUBMIT_PATH, json=submission_body())

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "The household, policy or catalog version does not exist for this country."
    )


class UnreachableWorkers:
    """Household workers whose manifest cannot be read."""

    def resolve(self, country: str, policyengine_version: str):
        """Fail as an unreachable manifest store does."""
        raise HouseholdWorkersUnavailableError(country, policyengine_version)


def test_an_unreadable_manifest_asks_the_caller_to_retry(
    household_client, harness
) -> None:
    harness.service.workers = UnreachableWorkers()

    response = household_client.post(SUBMIT_PATH, json=submission_body())

    assert response.status_code == 503
    assert response.headers["retry-after"]


def test_without_household_calculations_the_routes_are_unavailable() -> None:
    app = create_app(
        settings=make_settings(),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
    )
    with TestClient(app) as test_client:
        submitted = test_client.post(SUBMIT_PATH, json=submission_body())
        polled = test_client.get(f"{SUBMIT_PATH}/{uuid4()}")

    assert submitted.status_code == 503
    assert polled.status_code == 503


def test_the_v2_modal_environment_is_the_call_report_environment() -> None:
    settings = make_settings(
        call_report_modal_workspace_id="ac-test", call_report_modal_environment="main"
    )

    assert settings.v2_modal_environment == "main"
    assert make_settings().v2_modal_environment == ""


def test_the_simulation_database_builds_the_household_service() -> None:
    database = SimulationDatabase.from_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )
    try:
        service = database.household_calculations("staging")
    finally:
        database.dispose()

    assert isinstance(service, HouseholdCalculationService)
    assert isinstance(service.workers, ManifestHouseholdWorkers)
