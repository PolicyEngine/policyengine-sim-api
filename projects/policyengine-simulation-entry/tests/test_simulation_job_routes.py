"""HTTP routes of the simulation job store: status reads, collection and sweep.

None of them is in the OpenAPI document, and no production caller uses them
yet. The internal routes accept only Google OIDC tokens of the configured
invoker service account (Cloud Tasks and Cloud Scheduler).
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.google_oidc import InternalCallerRejectedError
from policyengine_simulation_entry.schemas import CallerIdentity
from policyengine_simulation_entry.simulation_jobs import (
    CallInspectionError,
    CallRunning,
    CallSucceeded,
    SimulationJobService,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from simulation_job_support import (
    ECONOMY_REPORT_KIND,
    HOUSEHOLD_KIND,
    TARGET,
    FakeDispatcher,
    FakeInspector,
    FakeQueue,
    InMemoryJobStore,
    SteppingClock,
)

VALID_TOKEN = "google-signed-token"
OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000001"


class FakeVerifier:
    """Accept only VALID_TOKEN, as the Google OIDC verifier would."""

    def __init__(self):
        self.tokens = []

    def verify(self, token):
        self.tokens.append(token)
        if token != VALID_TOKEN:
            msg = "InvalidAudienceError"
            raise InternalCallerRejectedError(msg)


def _service(inspector=None, queue=None):
    clock = SteppingClock()
    store = InMemoryJobStore(clock)
    service = SimulationJobService(
        store=store,
        dispatcher=FakeDispatcher(),
        inspector=inspector or FakeInspector(CallRunning()),
        queue=queue or FakeQueue(),
        job_kinds={
            HOUSEHOLD_KIND.name: HOUSEHOLD_KIND,
            ECONOMY_REPORT_KIND.name: ECONOMY_REPORT_KIND,
        },
        clock=clock,
    )
    return service, store, clock


def _running_job(service, kind=HOUSEHOLD_KIND.name):
    return asyncio.run(
        service.submit(
            kind=kind,
            request_payload={"household": {}},
            policyengine_version="6.7.5",
            submitted_by="api-v1@clients",
            target=TARGET,
            observability_id=OBSERVABILITY_ID,
        )
    )


def _client(service=None, verifier=None, **app_options):
    return TestClient(
        create_app(
            settings=make_settings(),
            backend=FakeBackend(),
            auth_dependency=app_options.pop("auth_dependency", lambda: None),
            simulation_jobs=service,
            internal_caller_verifier=verifier,
            **app_options,
        )
    )


def _bearer(token=VALID_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- Status reads -----------------------------------------------------------------


def test_status_reads_return_the_stored_result_from_the_database():
    service, _, _ = _service(
        inspector=FakeInspector(CallSucceeded({"net_income": 2.5}))
    )
    job = _running_job(service)
    asyncio.run(service.collect(job.id))
    service.inspector = FakeInspector(failure=AssertionError("polling called Modal"))

    with _client(service) as client:
        by_id = client.get(f"/simulation-jobs/{job.id}")
        by_call = client.get(f"/simulation-jobs/{job.modal_call_id}")

    assert by_id.status_code == 200
    assert by_id.json() == by_call.json()
    body = by_id.json()
    assert body["job_id"] == str(job.id)
    assert body["status"] == "succeeded"
    assert body["result"] == {"net_income": 2.5}
    assert body["result_sha256"]
    assert body["error"] is None
    assert by_id.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID


def test_an_unfinished_job_reads_as_accepted_with_a_retry_hint():
    service, _, _ = _service()
    job = _running_job(service)

    with _client(service) as client:
        result = client.get(f"/simulation-jobs/{job.id}")

    assert result.status_code == 202
    assert result.headers["Retry-After"] == "5"
    assert result.json()["status"] == "running"
    assert result.json()["result"] is None


def test_an_unknown_job_reads_as_not_found():
    service, _, _ = _service()

    with _client(service) as client:
        assert client.get("/simulation-jobs/fc-unknown").status_code == 404
        assert client.get("/simulation-jobs/not-an-id").status_code == 404


def test_status_reads_only_log_a_missing_permission_by_default():
    service, _, _ = _service()
    job = _running_job(service)
    caller = CallerIdentity.model_validate(
        {"sub": "user", "iss": "https://issuer.example/", "aud": "x", "exp": 1}
    )

    with _client(service, auth_dependency=lambda: caller) as client:
        allowed_while_logging = client.get(f"/simulation-jobs/{job.id}")

    assert allowed_while_logging.status_code == 202


def test_status_reads_reject_callers_without_the_permission_when_enforced():
    service, _, _ = _service()
    job = _running_job(service)
    caller = CallerIdentity.model_validate(
        {"sub": "user", "iss": "https://issuer.example/", "aud": "x", "exp": 1}
    )
    app = create_app(
        settings=make_settings(enforce_permissions=True),
        backend=FakeBackend(),
        auth_dependency=lambda: caller,
        simulation_jobs=service,
    )

    with TestClient(app) as client:
        assert client.get(f"/simulation-jobs/{job.id}").status_code == 403


def test_job_routes_are_unavailable_without_the_job_store():
    with _client() as client:
        assert client.get("/simulation-jobs/fc-anything").status_code == 503
        assert (
            client.post(
                "/internal/simulation-jobs/sweep", headers=_bearer()
            ).status_code
            == 503
        )


# --- Collection and the sweep --------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, _bearer("forged"), {"Authorization": "Basic abc"}],
    ids=["missing", "rejected", "not-bearer"],
)
def test_internal_routes_reject_callers_without_a_valid_google_token(headers):
    service, _, _ = _service()
    job = _running_job(service)

    with _client(service, FakeVerifier()) as client:
        collect = client.post(
            f"/internal/simulation-jobs/{job.id}/collect", headers=headers
        )
        sweep = client.post("/internal/simulation-jobs/sweep", headers=headers)

    assert collect.status_code == 403
    assert sweep.status_code == 403


def test_the_collection_route_reports_what_collection_did():
    service, store, _ = _service(
        inspector=FakeInspector(CallSucceeded({"net_income": 3.0}))
    )
    job = _running_job(service)

    with _client(service, FakeVerifier()) as client:
        result = client.post(
            f"/internal/simulation-jobs/{job.id}/collect", headers=_bearer()
        )

    assert result.status_code == 200
    assert result.json() == {
        "outcome": "succeeded",
        "job_status": "succeeded",
        "next_check_seconds": None,
    }
    assert store.get_result(job.id).result == {"net_income": 3.0}


def test_the_collection_route_asks_cloud_tasks_to_retry_transient_failures():
    service, _, _ = _service(
        inspector=FakeInspector(failure=CallInspectionError("Modal unavailable"))
    )
    job = _running_job(service)

    with _client(service, FakeVerifier()) as client:
        result = client.post(
            f"/internal/simulation-jobs/{job.id}/collect", headers=_bearer()
        )

    assert result.status_code == 503
    assert result.headers["Retry-After"]


def test_the_sweep_route_reports_its_counts_and_fails_when_tasks_were_refused():
    service, _, clock = _service()
    _running_job(service)
    clock.advance(minutes=6)

    with _client(service, FakeVerifier()) as client:
        swept = client.post("/internal/simulation-jobs/sweep", headers=_bearer())
        service.queue = FakeQueue(refuse=True)
        clock.advance(minutes=6)
        refused = client.post("/internal/simulation-jobs/sweep", headers=_bearer())

    assert swept.status_code == 200
    assert swept.json() == {"abandoned": 0, "enqueued": 1, "enqueue_failures": 0}
    assert refused.status_code == 503
    assert refused.json()["enqueue_failures"] == 1


def test_job_routes_stay_out_of_the_openapi_document():
    paths = create_app(settings=make_settings(), backend=FakeBackend()).openapi()[
        "paths"
    ]

    assert not [path for path in paths if "simulation-jobs" in path]


def test_a_report_job_reads_its_aggregate_from_the_report_results():
    from report_aggregate_support import ECONOMY_AGGREGATE

    service, store, _ = _service(
        inspector=FakeInspector(CallSucceeded(ECONOMY_AGGREGATE))
    )
    job = _running_job(service, kind=ECONOMY_REPORT_KIND.name)
    asyncio.run(service.collect(job.id))

    with _client(service) as client:
        body = client.get(f"/simulation-jobs/{job.id}").json()

    stored = store.get_report_result(job.id)
    assert body["status"] == "succeeded"
    assert body["result"] == stored.aggregate
    assert body["result_sha256"] == stored.aggregate_sha256
