"""Tests for the budget-window batch submission and status endpoints."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from fixtures.gateway_responses import OBSERVABILITY_ID, expected_bundle
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER


class TestBudgetWindowBatchEndpoints:
    """Tests for budget-window batch gateway endpoints."""

    def test__given_bundled_population_dataset__then_batch_rejects_data_key(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
                "data": "populace_us_2024_acs_local",
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    def test__given_bundled_supporting_artifact__then_rejects_before_batch_submission(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
                "data": "calibration_diagnostics",
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    @pytest.mark.parametrize(
        "data_override",
        [
            {"data": "gs://external-bucket/file.h5"},
            {"data": "populace_us_2024@other-revision"},
            {"data_version": "other-revision"},
        ],
    )
    def test__given_unsupported_dataset__then_rejects_before_batch_submission(
        self, mock_modal, client: TestClient, data_override
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
                **data_override,
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    def test__given_no_dataset_override__then_parent_batch_omits_dataset(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
            },
        )

        assert response.status_code == 200
        assert "data" not in mock_modal["func"].last_payload

    def test__given_budget_window_submission__then_returns_parent_batch_job_id(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 2,
            },
        )

        assert response.status_code == 200
        assert mock_modal["func"].last_from_name_call == (
            "policyengine-simulation-py4-10-0",
            "run_budget_window_batch",
        )
        data = response.json()
        assert "observability_id" not in data
        generated_observability_id = response.headers[OBSERVABILITY_ID_HEADER]
        assert str(UUID(generated_observability_id)) == generated_observability_id
        assert data == {
            "batch_job_id": "mock-batch-job-id-123",
            "status": "submitted",
            "poll_url": "/budget-window-jobs/mock-batch-job-id-123",
            "country": "us",
            "version": "4.10.0",
            "resolved_app_name": "policyengine-simulation-py4-10-0",
            "policyengine_bundle": expected_bundle("us", "1.500.0"),
        }

    @pytest.mark.usefixtures("mock_modal")
    def test__given_unknown_budget_window_job__then_records_handled_404(
        self, client: TestClient, monkeypatch
    ):
        recorded_errors = []
        monkeypatch.setattr(
            client.app.state.policyengine_observability,
            "record_exception",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get("/budget-window-jobs/missing-batch")

        assert response.status_code == 404
        assert response.json()["detail"] == (
            "Budget-window job not found: missing-batch"
        )
        assert str(recorded_errors[0][0]) == (
            "Budget-window job not found: missing-batch"
        )
        assert recorded_errors[0][1] == {
            "handled": True,
            "status_code": 404,
        }

    def test__given_parent_lookup_failure__then_degraded_poll_is_not_an_error(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        """Serve the seed state when the parent lookup fails, without an error.

        Given a submitted batch whose parent FunctionCall lookup fails
        When polling batch status
        Then the gateway serves the seed state (202) and records a degradation
        event — NOT a request error, which would emit http_request_failed and
        error metrics contradicting the successful response.
        """
        from fixtures.gateway_endpoints import MockFunctionCall

        recorded_errors = []
        recorded_events = []
        monkeypatch.setattr(
            client.app.state.policyengine_observability,
            "record_exception",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )
        monkeypatch.setattr(
            client.app.state.policyengine_observability,
            "event",
            lambda event, **kwargs: recorded_events.append(
                (event, kwargs["attributes"])
            ),
        )

        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }
        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
            },
        )
        batch_job_id = submit_response.json()["batch_job_id"]
        MockFunctionCall.from_id_errors[batch_job_id] = RuntimeError(
            "transient modal control-plane error"
        )

        response = client.get(f"/budget-window-jobs/{batch_job_id}")

        assert response.status_code == 202
        assert response.json()["status"] == "submitted"
        assert recorded_errors == []
        assert recorded_events == [
            (
                "budget_window_parent_lookup_degraded",
                {
                    "batch_job_id": batch_job_id,
                    "error_type": "RuntimeError",
                    "parent_call_not_found": False,
                },
            )
        ]

    def test__given_budget_window_include_cliffs__then_returns_422(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "include_cliffs": True,
            },
        )

        assert response.status_code == 422
        assert "cliff impacts are not supported" in response.text
        assert mock_modal["func"].last_payload is None

    def test__given_budget_window_submission__then_initial_poll_returns_seed_state(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 2,
                "_telemetry": {
                    "submission_claim_id": "proc-123",
                    "capture_mode": "disabled",
                },
            },
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        response = client.get(
            f"/budget-window-jobs/{submit_response.json()['batch_job_id']}"
        )

        assert response.status_code == 202
        assert response.json() == {
            "status": "submitted",
            "progress": 0,
            "completed_years": [],
            "running_years": [],
            "queued_years": ["2026", "2027", "2028"],
            "failed_years": [],
            "child_jobs": {},
            "result": None,
            "error": None,
            "resolved_app_name": "policyengine-simulation-py4-10-0",
            "policyengine_bundle": expected_bundle("us", "1.500.0"),
        }
        assert response.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID

    @pytest.mark.usefixtures("mock_modal")
    def test__given_batch_state__then_poll_returns_completed_response(
        self, client: TestClient
    ):
        from policyengine_simulation_contract.budget_window_state import (
            put_batch_job_state,
        )
        from policyengine_simulation_contract.gateway_models import (
            BudgetWindowAnnualImpact,
            BudgetWindowBatchState,
            BudgetWindowResult,
            BudgetWindowTotals,
            PolicyEngineBundle,
        )

        put_batch_job_state(
            BudgetWindowBatchState(
                batch_job_id="mock-batch-job-id-123",
                status="complete",
                country="us",
                region="us",
                version="1.500.0",
                target="general",
                resolved_app_name="policyengine-simulation-py4-10-0",
                policyengine_bundle=PolicyEngineBundle(model_version="1.500.0"),
                start_year="2026",
                window_size=2,
                max_parallel=2,
                request_payload={"country": "us", "region": "us"},
                years=["2026", "2027"],
                queued_years=[],
                running_years=[],
                completed_years=["2026", "2027"],
                failed_years=[],
                child_jobs={},
                partial_annual_impacts={},
                result=BudgetWindowResult(
                    startYear="2026",
                    endYear="2027",
                    windowSize=2,
                    annualImpacts=[
                        BudgetWindowAnnualImpact(
                            year="2026",
                            taxRevenueImpact=10,
                            federalTaxRevenueImpact=7,
                            stateTaxRevenueImpact=3,
                            benefitSpendingImpact=5,
                            budgetaryImpact=15,
                        ),
                        BudgetWindowAnnualImpact(
                            year="2027",
                            taxRevenueImpact=11,
                            federalTaxRevenueImpact=8,
                            stateTaxRevenueImpact=3,
                            benefitSpendingImpact=6,
                            budgetaryImpact=17,
                        ),
                    ],
                    totals=BudgetWindowTotals(
                        taxRevenueImpact=21,
                        federalTaxRevenueImpact=15,
                        stateTaxRevenueImpact=6,
                        benefitSpendingImpact=11,
                        budgetaryImpact=32,
                    ),
                ),
                error=None,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:01+00:00",
                observability_id=OBSERVABILITY_ID,
            )
        )

        response = client.get("/budget-window-jobs/mock-batch-job-id-123")

        assert response.status_code == 200
        assert response.json()["status"] == "complete"
        assert response.json()["result"]["totals"]["budgetaryImpact"] == 32
        assert "observability_id" not in response.json()
        assert response.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID

    @pytest.mark.usefixtures("mock_modal")
    def test__given_non_integer_start_year__then_budget_window_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "year-2026",
                "window_size": 3,
            },
        )

        assert response.status_code == 422
        assert (
            response.json()["detail"][0]["msg"]
            == "Value error, start_year must be an integer year"
        )

    @pytest.mark.usefixtures("mock_modal")
    def test__given_end_year_past_2099__then_budget_window_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2099",
                "window_size": 2,
            },
        )

        assert response.status_code == 422
        assert (
            response.json()["detail"][0]["msg"]
            == "Value error, budget-window end_year must be 2099 or earlier"
        )

    @pytest.mark.usefixtures("mock_modal")
    def test__given_window_size_above_max__then_budget_window_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 76,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "window_size"]

    @pytest.mark.usefixtures("mock_modal")
    def test__given_missing_region__then_budget_window_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "region"]

    @pytest.mark.usefixtures("mock_modal")
    def test__given_non_general_target__then_budget_window_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "target": "cliff",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "target"]

    @pytest.mark.usefixtures("mock_modal")
    def test__given_max_parallel_above_active_limit__then_submit_returns_422(
        self, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 21,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "max_parallel"]

    def test__given_parent_call_raises__then_failure_persists_across_polls(
        self, mock_modal, client: TestClient
    ):
        """Persist a parent-call failure so the failed state survives re-polling.

        Regression test for #448: when the parent Modal FunctionCall raises
        on .get() the first poll flipped seed state to failed in-memory only.
        Subsequent polls re-read the unmodified seed and flipped back to
        "submitted". Verify the mutation is persisted so the terminal state
        survives a second poll.
        """
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
                "max_parallel": 2,
            },
        )
        batch_id = submit_response.json()["batch_job_id"]

        # Arm the mocked call so .get(timeout=0) raises a non-timeout error.
        call = mock_modal["func"].last_call
        call.error = RuntimeError("worker crashed")
        call.running = False

        first_poll = client.get(f"/budget-window-jobs/{batch_id}")
        assert first_poll.status_code == 500
        first_body = first_poll.json()
        assert first_body["status"] == "failed"
        # Error is redacted (#453); the correlation id is in the string so
        # operators can jump from the user's report to the server-side log.
        assert first_body["error"].startswith("Simulation failed")
        assert "correlation_id=" in first_body["error"]
        assert "worker crashed" not in first_body["error"]

        second_poll = client.get(f"/budget-window-jobs/{batch_id}")
        assert second_poll.status_code == 500, second_poll.json()
        second_body = second_poll.json()
        assert second_body["status"] == "failed"
        assert second_body["error"].startswith("Simulation failed")
