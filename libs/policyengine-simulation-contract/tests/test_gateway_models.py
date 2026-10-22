"""Tests for gateway Pydantic models."""

import json

import pytest
from policyengine_simulation_contract.gateway_models import (
    MAX_GATEWAY_REQUEST_BYTES,
    BatchChildJobStatus,
    BudgetWindowAnnualImpact,
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    BudgetWindowResult,
    BudgetWindowTotals,
    HealthResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    ReadinessResponse,
    SimulationRequest,
    VersionMap,
    VersionsResponse,
)
from pydantic import ValidationError


class TestPingRequest:
    """Tests for PingRequest model."""

    def test_ping_request_accepts_integer_value(self):
        """Given an integer value, a PingRequest is created with that value."""
        # Given
        value = 42

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == 42

    def test_ping_request_accepts_negative_value(self):
        """Given a negative integer, a PingRequest is created with that value."""
        # Given
        value = -10

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == -10

    def test_ping_request_rejects_non_integer(self):
        """Given a non-integer, creating a PingRequest raises ValidationError."""
        # Given
        value = "not_an_integer"

        # When/Then
        with pytest.raises(ValidationError):
            PingRequest(value=value)

    def test_ping_request_rejects_missing_value(self):
        """Given no value, creating a PingRequest raises ValidationError."""
        # When/Then
        with pytest.raises(ValidationError):
            PingRequest()


class TestPingResponse:
    """Tests for PingResponse model."""

    def test_ping_response_accepts_integer_incremented(self):
        """Given an incremented integer, a PingResponse is created with that value."""
        # Given
        incremented = 43

        # When
        response = PingResponse(incremented=incremented)

        # Then
        assert response.incremented == 43

    def test_ping_response_serializes_correctly(self):
        """Given a PingResponse, model_dump returns the expected structure."""
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
        """Given no country, creating a SimulationRequest raises ValidationError."""
        # When/Then
        with pytest.raises(ValidationError):
            SimulationRequest()

    def test_simulation_request_accepts_country_only(self):
        """Given only a country, a SimulationRequest is created with version None."""
        # Given
        country = "us"

        # When
        request = SimulationRequest(country=country)

        # Then
        assert request.country == "us"
        assert request.version is None

    def test_simulation_request_accepts_country_and_version(self):
        """Given a country and version, a SimulationRequest keeps both values."""
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

    @pytest.mark.parametrize(
        "field",
        ["observability_id", "run_id", "request_id", "traceparent"],
    )
    def test_simulation_request_discards_noncanonical_context_fields(self, field):
        request = SimulationRequest(
            country="us",
            _telemetry={
                field: "identifier",
                "submission_claim_id": "claim-123",
                "capture_mode": "disabled",
            },
        )

        assert request.telemetry is not None
        assert request.telemetry.submission_claim_id == "claim-123"
        assert field not in request.telemetry.model_dump()

    def test_simulation_request_maps_legacy_process_id(self):
        request = SimulationRequest(
            country="us",
            _telemetry={"process_id": "legacy-claim-123"},
        )

        assert request.telemetry is not None
        assert request.telemetry.submission_claim_id == "legacy-claim-123"
        assert "process_id" not in request.telemetry.model_dump()

    def test_simulation_request_accepts_documented_simulation_fields(self):
        """Accept and preserve the documented simulation fields.

        Given reform, region and scope, when creating a SimulationRequest, then
        the model accepts and preserves them.
        """
        # Given
        data = {
            "country": "us",
            "region": "enhanced_us",
            "reform": {"some.parameter": {"2024-01-01": True}},
            "scope": "macro",
        }

        # When
        request = SimulationRequest(**data)

        # Then
        assert request.country == "us"
        dumped = request.model_dump(exclude_none=True)
        assert dumped["region"] == "enhanced_us"
        assert dumped["reform"] == {"some.parameter": {"2024-01-01": True}}
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
        """Region and region_group are mutually exclusive."""
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

    @pytest.mark.parametrize("field", ["data", "data_version"])
    def test_simulation_request_rejects_dataset_overrides(self, field):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            SimulationRequest.model_validate({"country": "us", field: None})

        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BudgetWindowBatchRequest.model_validate(
                {
                    "country": "us",
                    "region": "us",
                    "start_year": "2026",
                    "window_size": 1,
                    field: "custom_dataset_label",
                }
            )

    def test_simulation_request_rejects_oversized_payload(self):
        """Reject payloads over the gateway maximum before parsing the reform.

        They should 422 before Pydantic allocates proxy objects for the nested
        reform dict.
        """
        giant_reform = {
            f"mock.parameter[{i}]": {"2024-01-01": i} for i in range(10_000)
        }
        with pytest.raises(ValidationError, match="too large"):
            SimulationRequest(country="us", reform=giant_reform)

    def test_simulation_request_accepts_payload_just_below_256kb(self):
        """Accept a payload just below the 256 KB cap (boundary from #450)."""
        payload = self._payload_with_encoded_size(MAX_GATEWAY_REQUEST_BYTES - 1)

        # Must not raise — this is the just-under-cap happy path.
        request = SimulationRequest(**payload)
        assert request.country == "us"
        assert request.reform is not None
        assert request.reform.root == payload["reform"]

    def test_simulation_request_rejects_payload_just_above_256kb(self):
        """Reject a payload a few bytes over the strict 262_144-byte cap.

        The ValidationError should report the payload as ``too large``.
        """
        payload = self._payload_with_encoded_size(MAX_GATEWAY_REQUEST_BYTES + 1)

        with pytest.raises(ValidationError, match="too large"):
            SimulationRequest(**payload)

    def test_simulation_request_accepts_typed_telemetry_envelope(self):
        """Given a telemetry envelope, a SimulationRequest validates and keeps it."""
        request = SimulationRequest(
            country="us",
            _telemetry={
                "submission_claim_id": "proc-123",
                "capture_mode": "disabled",
            },
        )

        assert request.telemetry is not None
        assert request.telemetry.submission_claim_id == "proc-123"


class TestJobSubmitResponse:
    """Tests for JobSubmitResponse model."""

    def test_job_submit_response_creates_with_all_fields(self):
        """Given all required fields, a JobSubmitResponse is created correctly."""
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


class TestSimulationRequestBreakdown:
    """``breakdown`` asks a UK-wide or nation run for its local areas only."""

    def test_breakdown_defaults_to_none(self):
        request = SimulationRequest(country="uk", scope="macro")

        assert request.breakdown is None
        assert "breakdown" not in request.model_dump(exclude_none=True)

    @pytest.mark.parametrize("region", [None, "uk", "UK", "country/wales"])
    def test_local_areas_are_accepted_for_a_uk_wide_or_nation_region(self, region):
        # The Public API always sends include_cliffs: false.
        request = SimulationRequest(
            country="uk",
            scope="macro",
            region=region,
            include_cliffs=False,
            breakdown="local_areas",
        )

        assert request.breakdown == "local_areas"
        assert request.model_dump(exclude_none=True)["breakdown"] == "local_areas"

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"country": "us"}, "only for UK requests"),
            ({"region": "constituency/E14001063"}, "'constituency/E14001063'"),
            ({"region": "local_authority/E06000063"}, "'local_authority/E06000063'"),
            ({"region": "wales"}, "'wales'"),
            ({"region": "country/"}, "'country/'"),
            ({"include_cliffs": True}, "include_cliffs"),
            ({"segmented": True}, "segmented"),
            ({"region_group": ["country/wales"]}, "region_group"),
        ],
    )
    def test_local_areas_are_rejected_outside_a_uk_wide_or_nation_run(
        self, fields, message
    ):
        payload = {
            "country": "uk",
            "scope": "macro",
            "breakdown": "local_areas",
            **fields,
        }

        with pytest.raises(ValidationError) as caught:
            SimulationRequest.model_validate(payload)

        (error,) = caught.value.errors()
        assert error["type"] == "value_error"
        assert "breakdown" in error["msg"]
        assert message in error["msg"]

    def test_an_unknown_breakdown_is_rejected(self):
        with pytest.raises(ValidationError) as caught:
            SimulationRequest(country="uk", scope="macro", breakdown="districts")

        (error,) = caught.value.errors()
        assert error["type"] == "literal_error"
        assert error["loc"] == ("breakdown",)

    def test_budget_window_requests_take_no_breakdown(self):
        with pytest.raises(ValidationError) as caught:
            BudgetWindowBatchRequest.model_validate(
                {
                    "country": "uk",
                    "region": "uk",
                    "start_year": "2026",
                    "window_size": 1,
                    "breakdown": "local_areas",
                }
            )

        (error,) = caught.value.errors()
        assert error["type"] == "extra_forbidden"
        assert error["loc"] == ("breakdown",)


class TestJobStatusResponse:
    """Tests for JobStatusResponse model."""

    def test_job_status_response_rejects_an_unstructured_result(self):
        """Reject a completed result that does not match the macro output schema.

        Given a completed job with such a result, when creating a
        JobStatusResponse, then validation fails instead of silently accepting
        an opaque mapping.
        """
        with pytest.raises(ValidationError):
            JobStatusResponse(
                status="complete",
                result={"budget": {"total": 1000000}},
            )

    def test_job_status_response_running_without_result(self):
        """Given a running job, a JobStatusResponse has no result and no error."""
        # When
        response = JobStatusResponse(status="running")

        # Then
        assert response.status == "running"
        assert response.result is None
        assert response.error is None

    def test_job_status_response_failed_with_error(self):
        """Given a failed job with a message, a JobStatusResponse captures the error."""
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


class TestOperationalResponses:
    def test_versions_response_has_named_maps(self):
        response = VersionsResponse(
            policyengine=VersionMap({"latest": "4.10.0"}),
            us=VersionMap({"latest": "1.500.0"}),
            uk=VersionMap({"latest": "2.66.0"}),
        )

        assert response.model_dump(mode="json") == {
            "spm_capabilities": {},
            "policyengine": {"latest": "4.10.0"},
            "us": {"latest": "1.500.0"},
            "uk": {"latest": "2.66.0"},
            "bundles": {},
        }

    def test_health_and_readiness_statuses_are_constrained(self):
        assert HealthResponse().status == "healthy"
        assert ReadinessResponse(status="ready").status == "ready"

        with pytest.raises(ValidationError):
            ReadinessResponse(status="unknown")


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
                "submission_claim_id": "proc-123",
                "capture_mode": "disabled",
            },
        )

        assert request.telemetry is not None
        assert request.telemetry.submission_claim_id == "proc-123"


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
                "spm": None,
                "dataset": "default",
                "dataset_sha256": None,
            },
        }


class TestBudgetWindowBatchStatusResponse:
    """Tests for budget-window batch status responses."""

    def test_annual_impact_rejects_legacy_sibling_spm_config(self):
        with pytest.raises(ValidationError):
            BudgetWindowAnnualImpact.model_validate(
                {
                    "spm_config": {"scenario": "ce_trend"},
                    "year": "2026",
                    "taxRevenueImpact": 10,
                    "federalTaxRevenueImpact": 8,
                    "stateTaxRevenueImpact": 2,
                    "benefitSpendingImpact": -3,
                    "budgetaryImpact": 13,
                }
            )

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
            "errors": None,
        }
        assert dumped["result"]["kind"] == "budgetWindow"


def test_policyengine_bundle_dataset_sha256_is_optional_and_hex():
    import pytest
    from policyengine_simulation_contract.gateway_models import PolicyEngineBundle
    from pydantic import ValidationError

    assert PolicyEngineBundle(model_version="2.126.8").dataset_sha256 is None
    assert (
        PolicyEngineBundle(
            model_version="2.126.8", dataset_sha256="a" * 64
        ).dataset_sha256
        == "a" * 64
    )
    for bad in ("A" * 64, "a" * 63, "not-a-digest"):
        with pytest.raises(ValidationError):
            PolicyEngineBundle(model_version="2.126.8", dataset_sha256=bad)


def test_versions_response_bundles_default_to_empty():
    from policyengine_simulation_contract.gateway_models import VersionsResponse

    response = VersionsResponse(policyengine={}, us={}, uk={})
    assert response.bundles == {}
