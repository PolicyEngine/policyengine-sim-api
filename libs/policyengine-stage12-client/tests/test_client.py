from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    SimulationRole,
)

from policyengine_stage12_client import Stage12PersistenceClient

NOW = datetime(2026, 9, 22, tzinfo=UTC)
REPORT_ID = UUID("00000000-0000-0000-0000-000000000001")
SIMULATION_ID = UUID("00000000-0000-0000-0000-000000000002")


def _report() -> ComparisonReportRecord:
    return ComparisonReportRecord(
        evaluation_id=REPORT_ID,
        status=ComparisonRunLifecycleStatus.PENDING,
        aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
        environment="staging",
        calculation_flow="economy",
        originating_request_id="request-1",
        production_identity="job-1",
        incumbent_execution_id="job-1",
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        report_coordinator_callable="coordinate_report",
        version_manifest_sha256="a" * 64,
        policyengine_version="5.2.0",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        country="us",
        dataset_identity="populace_us_2024",
        dataset_uri="hf://policyengine/data/populace_us_2024.h5@revision",
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_artifact_revision="revision",
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _simulation() -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord(
        simulation_execution_id=SIMULATION_ID,
        evaluation_id=REPORT_ID,
        role=SimulationRole.BASELINE,
        input_sha256="b" * 64,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="a" * 64,
        modal_invocation_id="dispatch-pending-1",
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _client(handler):
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    http_client = httpx.Client(transport=httpx.MockTransport(capture))
    client = Stage12PersistenceClient(
        "https://api.example",
        token_provider=lambda: "signed-token",
        http_client=http_client,
    )
    return client, requests


def test_report_operations_use_authenticated_internal_api() -> None:
    report = _report()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer signed-token"
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"record": report.model_dump(mode="json"), "created": True},
            )
        return httpx.Response(200, json={"record": report.model_dump(mode="json")})

    client, requests = _client(handler)

    created = client.create_or_resolve_report(report)
    fetched = client.get_report(REPORT_ID)
    replaced = client.replace_report(report)
    compared = client.replace_report_result_comparison(report)

    assert created.record == report
    assert created.created is True
    assert fetched == report
    assert replaced == report
    assert compared == report
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/internal/stage12/comparison-runs/reports/resolve"),
        ("GET", f"/internal/stage12/comparison-runs/reports/{REPORT_ID}"),
        (
            "PUT",
            f"/internal/stage12/comparison-runs/reports/{REPORT_ID}/lifecycle",
        ),
        (
            "PUT",
            f"/internal/stage12/comparison-runs/reports/{REPORT_ID}/comparison",
        ),
    ]


def test_simulation_operations_use_authenticated_internal_api() -> None:
    simulation = _simulation()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/simulations"):
            return httpx.Response(
                200,
                json={"items": [simulation.model_dump(mode="json")]},
            )
        if request.url.path.endswith("/resolve"):
            return httpx.Response(
                200,
                json={
                    "record": simulation.model_dump(mode="json"),
                    "created": True,
                },
            )
        return httpx.Response(
            200,
            json={"record": simulation.model_dump(mode="json")},
        )

    client, requests = _client(handler)

    created = client.create_or_resolve_simulation(simulation)
    fetched = client.get_simulation(SIMULATION_ID)
    listed = client.list_simulations(REPORT_ID)
    replaced = client.replace_simulation(simulation)
    attached = client.attach_simulation_invocation(
        SIMULATION_ID,
        expected_placeholder="dispatch-pending-1",
        modal_invocation_id="fc-123",
        updated_at=NOW,
    )

    assert created.record == simulation
    assert created.created is True
    assert fetched == simulation
    assert listed == (simulation,)
    assert replaced == simulation
    assert attached == simulation
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/internal/stage12/comparison-runs/simulations/resolve"),
        (
            "GET",
            f"/internal/stage12/comparison-runs/simulations/{SIMULATION_ID}",
        ),
        (
            "GET",
            f"/internal/stage12/comparison-runs/reports/{REPORT_ID}/simulations",
        ),
        (
            "PUT",
            f"/internal/stage12/comparison-runs/simulations/{SIMULATION_ID}/lifecycle",
        ),
        (
            "POST",
            f"/internal/stage12/comparison-runs/simulations/{SIMULATION_ID}/invocation",
        ),
    ]
    invocation_request = requests[-1].read().decode()
    assert "dispatch-pending-1" in invocation_request
    assert "fc-123" in invocation_request


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [(404, LookupError), (409, ValueError), (503, RuntimeError)],
)
def test_error_mapping_does_not_expose_response_body(
    status_code: int,
    expected_error: type[Exception],
) -> None:
    client, _ = _client(
        lambda _: httpx.Response(status_code, text="sensitive persistence detail")
    )

    with pytest.raises(expected_error) as error:
        client.get_report(REPORT_ID)

    assert "sensitive" not in str(error.value)


def test_invalid_success_response_is_sanitized() -> None:
    client, _ = _client(
        lambda _: httpx.Response(200, json={"record": {"secret": "value"}})
    )

    with pytest.raises(RuntimeError) as error:
        client.get_report(REPORT_ID)

    assert "secret" not in str(error.value)
