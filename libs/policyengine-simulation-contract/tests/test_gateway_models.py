"""Tests for gateway Pydantic models."""

import json

import pytest
from pydantic import ValidationError

from policyengine_simulation_contract.gateway_models import (
    BatchChildJobStatus,
    BudgetWindowAnnualImpact,
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    BudgetWindowResult,
    BudgetWindowTotals,
    JobStatusResponse,
    JobSubmitResponse,
    MAX_GATEWAY_REQUEST_BYTES,
    PingRequest,
    PingResponse,
    SimulationRequest,
)


class TestPingRequest:
    """Tests for PingRequest model."""

    def test_ping_request_accepts_integer_value(self):
        """
        Given an integer value
        When creating a PingRequest
        Then the model is created with the value.
        """
        # Given
        value = 42

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == 42

    def test_ping_request_accepts_negative_value(self):
        """
        Given a negative integer value
        When creating a PingRequest
        Then the model is created with the negative value.
        """
        # Given
        value = -10

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == -10

    def test_ping_request_rejects_non_integer(self):
        """
        Given a non-integer value
        When creating a PingRequest
        Then a ValidationError is raised.
        """
        # Given
        value = "not_an_integer"

        # When/Then
        with pytest.raises(ValidationError):
            PingRequest(value=value)

    def test_ping_request_rejects_missing_value(self):
        """
        Given no value
        When creating a PingRequest
        Then a ValidationError is raised.
        """
        # When/Then
        with pytest.raises(ValidationError):
            PingRequest()


class TestPingResponse:
    """Tests for PingResponse model."""

    def test_ping_response_accepts_integer_incremented(self):
        """
        Given an integer incremented value
        When creating a PingResponse
        Then the model is created with the value.
        """
        # Given
        incremented = 43

        # When
        response = PingResponse(incremented=incremented)

        # Then
        assert response.incremented == 43

    def test_ping_response_serializes_correctly(self):
        """
        Given a PingResponse
        When converting to dict
        Then the correct structure is returned.
        """
        # Given
        response = PingResponse(incremented=100)

        # When
        result = response.model_dump()

        # Then
        assert result == {"incremented": 100}


class TestSimulationRequest:
    """Tests for SimulationRequest model."""

    @staticmethod
    def _payload_with_encoded_size(target_size: int) -> dict:
        payload = {"country": "us", "reform": {"mock.parameter": {"2024-01-01": ""}}}
        base_size = len(json.dumps(payload, default=str))
        payload["reform"]["mock.parameter"]["2024-01-01"] = "x" * (
            target_size - base_size
        )
        assert len(json.dumps(payload, default=str)) == target_size
        return payload

    def test_simulation_request_requires_country(self):
        """
        Given no country
        When creating a SimulationRequest
        Then a ValidationError is raised.
        """
        # When/Then
        with pytest.raises(ValidationError):
            SimulationRequest()

    def test_simulation_request_accepts_country_only(self):
        """
        Given only a country
        When creating a SimulationRequest
        Then the model is created with version as None.
        """
        # Given
        country = "us"

        # When
        request = SimulationRequest(country=country)

        # Then
        assert request.country == "us"
        assert request.version is None

    def test_simulation_request_accepts_country_and_version(self):
        """
        Given a country and version
        When creating a SimulationRequest
        Then the model is created with both values.
        """
        # Given
        country = "uk"
        version = "1.0.0"

        # When
        request = SimulationRequest(country=country, version=version)

        # Then
        assert request.country == "uk"
        assert request.version == "1.0.0"

    def test_simulation_request_accepts_policyengine_version(self):
        request = SimulationRequest(country="uk", policyengine_version="4.10.0")

        assert request.country == "uk"
        assert request.version is None
        assert request.policyengine_version == "4.10.0"

    def test_simulation_request_accepts_documented_simulation_fields(self):
        """
        Given the documented simulation fields (reform, region, data, ...)
        When creating a SimulationRequest
        Then the model accepts and preserves them.
        """
        # Given
        data = {
            "country": "us",
            "region": "enhanced_us",
            "reform": {"some.parameter": {"2024-01-01": True}},
            "data": "custom_dataset_label",
            "scope": "macro",
        }

        # When
        request = SimulationRequest(**data)

        # Then
        assert request.country == "us"
        dumped = request.model_dump(exclude_none=True)
        assert dumped["region"] == "enhanced_us"
        assert dumped["reform"] == {"some.parameter": {"2024-01-01": True}}
        assert dumped["data"] == "custom_dataset_label"
        assert dumped["scope"] == "macro"

    def test_simulation_request_accepts_region_group(self):
        """A region_group (list of member region codes) is accepted and preserved."""
        request = SimulationRequest(
            country="us",
            region_group=["state/hi", "state/ia", "state/wi"],
            scope="macro",
        )
        assert request.region_group == ["state/hi", "state/ia", "state/wi"]
        assert request.region is None

    def test_simulation_request_accepts_neither_region_nor_region_group(self):
        """Neither region nor region_group is valid (a national run)."""
        request = SimulationRequest(country="us", scope="macro")
        assert request.region is None
        assert request.region_group is None

    def test_simulation_request_rejects_region_and_region_group_together(self):
        """region and region_group are mutually exclusive."""
        with pytest.raises(ValidationError, match="not both"):
            SimulationRequest(
                country="us", region="state/hi", region_group=["state/ia"]
            )

    def test_simulation_request_segmented_defaults_to_none(self):
        """Absent segmented means the default (segmented where eligible)."""
        request = SimulationRequest(country="us", scope="macro")
        assert request.segmented is None

    def test_simulation_request_accepts_segmented_opt_out(self):
        """segmented=False (force monolithic) round-trips."""
        request = SimulationRequest(country="us", scope="macro", segmented=False)
        assert request.segmented is False
        assert request.model_dump(exclude_none=True)["segmented"] is False

    def test_simulation_request_accepts_segmented_true(self):
        """An explicit segmented=True is accepted (same as the default)."""
        request = SimulationRequest(country="us", scope="macro", segmented=True)
        assert request.segmented is True

    def test_simulation_request_rejects_unknown_fields(self):
        """Unknown fields should fail fast with ``extra="forbid"``."""
        with pytest.raises(ValidationError):
            SimulationRequest(country="us", dataset="custom_dataset_label")
        with pytest.raises(ValidationError):
            SimulationRequest(country="us", mystery_flag=True)

    def test_simulation_request_rejects_oversized_payload(self):
        """Payloads that exceed the gateway max should 422 before Pydantic
        allocates proxy objects for the nested reform dict."""
        giant_reform = {
            f"mock.parameter[{i}]": {"2024-01-01": i} for i in range(10_000)
        }
        with pytest.raises(ValidationError, match="too large"):
            SimulationRequest(country="us", reform=giant_reform)

    def test_simulation_request_accepts_payload_just_below_256kb(self):
        """256 KB boundary (#450): a payload just below the cap must be
        accepted."""
        payload = self._payload_with_encoded_size(MAX_GATEWAY_REQUEST_BYTES - 1)

        # Must not raise — this is the just-under-cap happy path.
        request = SimulationRequest(**payload)
        assert request.country == "us"
        assert request.reform == payload["reform"]

    def test_simulation_request_rejects_payload_just_above_256kb(self):
        """The cap is strict: a payload that crosses 262_144 bytes by even a
        few bytes should be rejected with a ``too large`` ValidationError."""
        payload = self._payload_with_encoded_size(MAX_GATEWAY_REQUEST_BYTES + 1)

        with pytest.raises(ValidationError, match="too large"):
            SimulationRequest(**payload)

    def test_simulation_request_accepts_typed_telemetry_envelope(self):
        """
        Given a telemetry envelope
        When creating a SimulationRequest
        Then the envelope is validated and preserved.
        """
        request = SimulationRequest(
            country="us",
            _telemetry={
                "run_id": "run-123",
                "process_id": "proc-123",
                "capture_mode": "disabled",
            },
        )

        assert request.telemetry is not None
        assert request.telemetry.run_id == "run-123"
        assert request.telemetry.process_id == "proc-123"


class TestJobSubmitResponse:
    """Tests for JobSubmitResponse model."""

    def test_job_submit_response_creates_with_all_fields(self):
        """
        Given all required fields
        When creating a JobSubmitResponse
        Then the model is created correctly.
        """
        # Given
        data = {
            "job_id": "fc-abc123",
            "status": "submitted",
            "poll_url": "/jobs/fc-abc123",
            "country": "us",
            "version": "1.459.0",
            "resolved_app_name": "policyengine-simulation-py3-9-0",
            "policyengine_bundle": {
                "model_version": "1.459.0",
                "policyengine_version": None,
                "data_version": None,
                "dataset": "gs://external-bucket/custom/file.h5@custom-v1",
            },
        }

        # When
        response = JobSubmitResponse(**data)

        # Then
        assert response.job_id == "fc-abc123"
        assert response.status == "submitted"
        assert response.poll_url == "/jobs/fc-abc123"
        assert response.country == "us"
        assert response.version == "1.459.0"
        assert response.resolved_app_name == "policyengine-simulation-py3-9-0"
        assert response.policyengine_bundle.model_version == "1.459.0"
        assert response.policyengine_bundle.policyengine_version is None
        assert response.policyengine_bundle.dataset == (
            "gs://external-bucket/custom/file.h5@custom-v1"
        )


class TestJobStatusResponse:
    """Tests for JobStatusResponse model."""

    def test_job_status_response_complete_with_result(self):
        """
        Given a completed job with result
        When creating a JobStatusResponse
        Then the model contains the result.
        """
        # Given
        result = {"budget": {"total": 1000000}}

        # When
        response = JobStatusResponse(status="complete", result=result)

        # Then
        assert response.status == "complete"
        assert response.result == {"budget": {"total": 1000000}}
        assert response.error is None

    def test_job_status_response_running_without_result(self):
        """
        Given a running job
        When creating a JobStatusResponse
        Then result and error are None.
        """
        # When
        response = JobStatusResponse(status="running")

        # Then
        assert response.status == "running"
        assert response.result is None
        assert response.error is None

    def test_job_status_response_failed_with_error(self):
        """
        Given a failed job with error message
        When creating a JobStatusResponse
        Then the error is captured.
        """
        # Given
        error_msg = "Simulation timed out"

        # When
        response = JobStatusResponse(status="failed", error=error_msg)

        # Then
        assert response.status == "failed"
        assert response.result is None
        assert response.error == "Simulation timed out"

    def test_job_status_response_accepts_bundle_metadata(self):
        response = JobStatusResponse(
            status="complete",
            result={"budget": {"total": 1000000}},
            resolved_app_name="policyengine-simulation-py3-9-0",
            policyengine_bundle={
                "model_version": "1.459.0",
                "policyengine_version": None,
                "data_version": None,
                "dataset": "gs://external-bucket/custom/file.h5@custom-v1",
            },
        )

        assert response.resolved_app_name == "policyengine-simulation-py3-9-0"
        assert response.policyengine_bundle is not None
        assert response.policyengine_bundle.dataset == (
            "gs://external-bucket/custom/file.h5@custom-v1"
        )


class TestBudgetWindowBatchRequest:
    """Tests for budget-window batch request validation."""

    def test_budget_window_batch_request_requires_region(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(
                country="us",
                start_year="2026",
                window_size=10,
            )

    def test_budget_window_batch_request_requires_start_year(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(country="us", region="us", window_size=10)

    def test_budget_window_batch_request_requires_positive_window_size(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2026",
                window_size=0,
            )

    def test_budget_window_batch_request_rejects_window_size_above_max_limit(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2026",
                window_size=76,
            )

    def test_budget_window_batch_request_rejects_end_year_past_2099(self):
        with pytest.raises(
            ValidationError, match="budget-window end_year must be 2099 or earlier"
        ):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2099",
                window_size=2,
            )

    def test_budget_window_batch_request_requires_integer_like_start_year(self):
        with pytest.raises(ValidationError, match="start_year must be an integer year"):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="year-2026",
                window_size=3,
            )

    def test_budget_window_batch_request_rejects_non_general_target(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2026",
                window_size=3,
                target="cliff",
            )

    def test_budget_window_batch_request_rejects_include_cliffs(self):
        with pytest.raises(ValidationError, match="cliff impacts are not supported"):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2026",
                window_size=3,
                include_cliffs=True,
            )

    def test_budget_window_batch_request_rejects_max_parallel_above_active_limit(self):
        with pytest.raises(ValidationError):
            BudgetWindowBatchRequest(
                country="us",
                region="us",
                start_year="2026",
                window_size=3,
                max_parallel=21,
            )

    def test_budget_window_batch_request_accepts_extra_simulation_fields(self):
        request = BudgetWindowBatchRequest(
            country="us",
            region="us",
            start_year="2026",
            window_size=10,
            max_parallel=2,
            scope="macro",
            reform={},
        )

        dumped = request.model_dump()
        assert dumped["scope"] == "macro"
        assert dumped["reform"] == {}
        assert request.max_parallel == 2
        assert request.start_year == "2026"

    def test_budget_window_batch_request_accepts_internal_telemetry_alias(self):
        request = BudgetWindowBatchRequest(
            country="us",
            region="us",
            start_year="2026",
            window_size=10,
            _telemetry={
                "run_id": "batch-run-123",
                "process_id": "proc-123",
                "capture_mode": "disabled",
            },
        )

        assert request.telemetry is not None
        assert request.telemetry.run_id == "batch-run-123"


class TestBudgetWindowBatchSubmitResponse:
    """Tests for budget-window batch submit responses."""

    def test_budget_window_batch_submit_response_serializes_correctly(self):
        response = BudgetWindowBatchSubmitResponse(
            batch_job_id="bw-123",
            status="submitted",
            poll_url="/budget-window-jobs/bw-123",
            country="us",
            version="1.500.0",
            resolved_app_name="policyengine-simulation-py4-10-0",
            policyengine_bundle={
                "model_version": "1.500.0",
                "dataset": "default",
            },
            run_id="batch-run-123",
        )

        assert response.model_dump(mode="json") == {
            "batch_job_id": "bw-123",
            "status": "submitted",
            "poll_url": "/budget-window-jobs/bw-123",
            "country": "us",
            "version": "1.500.0",
            "resolved_app_name": "policyengine-simulation-py4-10-0",
            "policyengine_bundle": {
                "model_version": "1.500.0",
                "policyengine_version": None,
                "data_version": None,
                "dataset": "default",
            },
            "run_id": "batch-run-123",
        }


class TestBudgetWindowBatchStatusResponse:
    """Tests for budget-window batch status responses."""

    def test_budget_window_batch_status_response_accepts_child_jobs_and_result(self):
        response = BudgetWindowBatchStatusResponse(
            status="complete",
            progress=100,
            completed_years=["2026", "2027"],
            running_years=[],
            queued_years=[],
            failed_years=[],
            child_jobs={
                "2026": BatchChildJobStatus(job_id="fc-2026", status="complete"),
                "2027": BatchChildJobStatus(job_id="fc-2027", status="complete"),
            },
            result=BudgetWindowResult(
                startYear="2026",
                endYear="2027",
                windowSize=2,
                annualImpacts=[
                    BudgetWindowAnnualImpact(
                        year="2026",
                        taxRevenueImpact=10,
                        federalTaxRevenueImpact=8,
                        stateTaxRevenueImpact=2,
                        benefitSpendingImpact=-3,
                        budgetaryImpact=13,
                    )
                ],
                totals=BudgetWindowTotals(
                    taxRevenueImpact=10,
                    federalTaxRevenueImpact=8,
                    stateTaxRevenueImpact=2,
                    benefitSpendingImpact=-3,
                    budgetaryImpact=13,
                ),
            ),
        )

        dumped = response.model_dump(mode="json")
        assert dumped["status"] == "complete"
        assert dumped["progress"] == 100
        assert dumped["completed_years"] == ["2026", "2027"]
        assert dumped["child_jobs"]["2026"] == {
            "job_id": "fc-2026",
            "status": "complete",
            "error": None,
        }
        assert dumped["result"]["kind"] == "budgetWindow"
