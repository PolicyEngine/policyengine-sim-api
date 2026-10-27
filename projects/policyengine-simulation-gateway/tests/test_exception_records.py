"""Client errors the gateway answers with 4xx record no exception; 5xx still do.

``ObservabilityRuntime.record_exception`` always writes an ERROR record. A
request the gateway answers with a 4xx already gets a ``request.completed``
record whose outcome is ``client_error``, so an exception record for it adds
only a false alarm to the daily error report. A server failure (5xx) keeps
its exception record.
"""

from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from policyengine_simulation_gateway import job_creation_routes

# The country the deploy pipeline's authentication test sends, expecting 400.
AUTHENTICATION_PROBE_COUNTRY = "__authentication_probe__"


class Submission(NamedTuple):
    """A submission endpoint and the request fields it needs besides country."""

    path: str
    request_fields: dict[str, object]


SUBMISSIONS = [
    pytest.param(
        Submission(
            path="/simulate/economy/comparison",
            request_fields={"scope": "macro", "reform": {}},
        ),
        id="comparison",
    ),
    pytest.param(
        Submission(
            path="/simulate/economy/budget-window",
            request_fields={
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
            },
        ),
        id="budget_window",
    ),
]

RecordedException = tuple[BaseException, dict[str, object]]


@pytest.fixture
def recorded_exceptions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> list[RecordedException]:
    """Replace the runtime's ``record_exception`` with one that keeps each call."""
    recorded: list[RecordedException] = []

    def record_exception(error: BaseException, **keyword_arguments: object) -> None:
        recorded.append((error, keyword_arguments))

    monkeypatch.setattr(
        client.app.state.policyengine_observability,
        "record_exception",
        record_exception,
    )
    return recorded


@pytest.mark.parametrize("submission", SUBMISSIONS)
def test__given_unknown_country__then_returns_400_without_exception_record(
    mock_modal,
    client: TestClient,
    recorded_exceptions: list[RecordedException],
    submission: Submission,
):
    response = client.post(
        submission.path,
        json={"country": AUTHENTICATION_PROBE_COUNTRY, **submission.request_fields},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": f"Unknown country: {AUTHENTICATION_PROBE_COUNTRY}"
    }
    assert recorded_exceptions == []
    assert mock_modal["func"].calls == []


@pytest.mark.parametrize("submission", SUBMISSIONS)
def test__given_bundle_lookup_error__then_returns_400_without_exception_record(
    mock_modal,
    client: TestClient,
    recorded_exceptions: list[RecordedException],
    monkeypatch: pytest.MonkeyPatch,
    submission: Submission,
):
    bundle_lookup_message = "The requested bundle has no dataset for this request"

    def reject_bundle_lookup(*_arguments: object) -> None:
        raise ValueError(bundle_lookup_message)

    monkeypatch.setattr(
        job_creation_routes, "build_policyengine_bundle", reject_bundle_lookup
    )

    response = client.post(
        submission.path, json={"country": "us", **submission.request_fields}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": bundle_lookup_message}
    assert recorded_exceptions == []
    assert mock_modal["func"].calls == []


def test__given_modal_lookup_failure__then_job_status_records_server_exception(
    mock_modal,
    client: TestClient,
    recorded_exceptions: list[RecordedException],
):
    submit_response = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
    )
    job_id = submit_response.json()["job_id"]
    lookup_failure = RuntimeError("Modal control plane unavailable")
    mock_modal["function_call"].from_id_errors[job_id] = lookup_failure
    client_receiving_server_errors = TestClient(
        client.app, raise_server_exceptions=False
    )

    response = client_receiving_server_errors.get(f"/jobs/{job_id}")

    assert response.status_code == 500
    assert recorded_exceptions == [
        (lookup_failure, {"handled": False, "status_code": 500}),
    ]
