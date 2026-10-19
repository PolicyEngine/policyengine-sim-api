"""The Modal and Cloud Tasks adapters behind the simulation job service."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import modal
import pytest
from policyengine_simulation_entry import simulation_jobs as jobs_module
from policyengine_simulation_entry.simulation_jobs import (
    CallFailed,
    CallInspectionError,
    CallRunning,
    CallSucceeded,
    CloudTasksCollectionQueue,
    CollectionQueueError,
    ModalCallDispatcher,
    ModalCallInspector,
    ModalTarget,
)

JOB_ID = UUID("00000000-0000-4000-8000-0000000000aa")
QUEUE = "projects/policyengine-staging/locations/us-central1/queues/simulation-jobs"
TARGET_URL = "https://policyengine-simulation-entry-staging-abc-uc.a.run.app"
INVOKER = "simulation-jobs-invoker@policyengine-staging.iam.gserviceaccount.com"


# --- Modal call inspection --------------------------------------------------------


class FakeFunctionCall:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.timeouts = []
        self.get = SimpleNamespace(aio=self._get)

    async def _get(self, timeout=None):
        self.timeouts.append(timeout)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour


def _inspect(monkeypatch, behaviour):
    calls = {}

    def from_id(call_id):
        calls[call_id] = FakeFunctionCall(behaviour)
        return calls[call_id]

    monkeypatch.setattr(modal.FunctionCall, "from_id", from_id)
    outcome = asyncio.run(ModalCallInspector().inspect("fc-01ABC"))
    assert calls["fc-01ABC"].timeouts == [0]
    return outcome


def test_a_call_without_output_is_still_running(monkeypatch):
    assert _inspect(monkeypatch, TimeoutError()) == CallRunning()


def test_a_finished_call_returns_its_output(monkeypatch):
    assert _inspect(monkeypatch, {"net_income": 1.0}) == CallSucceeded(
        {"net_income": 1.0}
    )


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (modal.exception.OutputExpiredError(), "result_expired"),
        (modal.exception.FunctionTimeoutError(), "worker_timeout"),
        (modal.exception.NotFoundError(), "call_not_found"),
        (KeyError("household secret"), "worker_error"),
    ],
)
def test_a_failed_call_fails_with_a_code_and_no_worker_data(
    monkeypatch, failure, error_code
):
    outcome = _inspect(monkeypatch, failure)

    assert isinstance(outcome, CallFailed)
    assert outcome.error_code == error_code
    assert "secret" not in outcome.error_message


@pytest.mark.parametrize(
    "failure",
    [modal.exception.ConnectionError(), modal.exception.AuthError()],
)
def test_modal_being_unreachable_is_transient(monkeypatch, failure):
    with pytest.raises(CallInspectionError):
        _inspect(monkeypatch, failure)


# --- Modal dispatch ----------------------------------------------------------------


def test_the_dispatcher_spawns_the_target_function_with_observability_context(
    monkeypatch,
):
    looked_up = []
    spawned = []

    async def spawn(payload, *, observability_context):
        spawned.append((payload, observability_context))
        return SimpleNamespace(object_id="fc-01NEW")

    def from_name(application, function, *, environment_name):
        looked_up.append((application, function, environment_name))
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))

    monkeypatch.setattr(modal.Function, "from_name", from_name)
    target = ModalTarget(application="app-v2", function="calculate_household")

    call_id = asyncio.run(
        ModalCallDispatcher("staging").spawn(
            target, {"household": {}}, observability_context={"traceparent": "x"}
        )
    )

    assert call_id == "fc-01NEW"
    assert looked_up == [("app-v2", "calculate_household", "staging")]
    assert spawned == [({"household": {}}, {"traceparent": "x"})]


def test_the_dispatcher_requires_an_acknowledged_call_id(monkeypatch):
    async def spawn(_payload, *, observability_context):
        return SimpleNamespace(object_id="")

    monkeypatch.setattr(
        modal.Function,
        "from_name",
        lambda *_args, **_options: SimpleNamespace(spawn=SimpleNamespace(aio=spawn)),
    )

    with pytest.raises(RuntimeError, match="no call ID"):
        asyncio.run(
            ModalCallDispatcher("staging").spawn(
                ModalTarget("app", "function"), {}, observability_context=None
            )
        )


# --- Cloud Tasks ---------------------------------------------------------------------


def _queue(handler, clock=None):
    async def access_token():
        return "access-token"

    return CloudTasksCollectionQueue(
        queue=QUEUE,
        target_url=TARGET_URL,
        invoker_service_account=INVOKER,
        access_token=access_token,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock or (lambda: datetime(2026, 10, 20, 12, tzinfo=UTC)),
    )


def test_a_collection_task_targets_the_stable_service_with_an_oidc_token():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"name": f"{QUEUE}/tasks/1"})

    asyncio.run(_queue(handler).enqueue(JOB_ID, delay_seconds=8))

    request = requests[0]
    assert str(request.url) == f"https://cloudtasks.googleapis.com/v2/{QUEUE}/tasks"
    assert request.headers["Authorization"] == "Bearer access-token"
    assert json.loads(request.content) == {
        "task": {
            "scheduleTime": "2026-10-20T12:00:08Z",
            "httpRequest": {
                "httpMethod": "POST",
                "url": f"{TARGET_URL}/internal/simulation-jobs/{JOB_ID}/collect",
                "oidcToken": {
                    "serviceAccountEmail": INVOKER,
                    "audience": TARGET_URL,
                },
            },
        }
    }


def test_an_immediate_task_has_no_schedule_time():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    asyncio.run(_queue(handler).enqueue(JOB_ID, delay_seconds=0))

    assert "scheduleTime" not in bodies[0]["task"]


def test_a_refused_or_unreachable_queue_raises_a_queue_error():
    refused = _queue(lambda _request: httpx.Response(403, json={"error": {}}))

    def unreachable(request):
        message = "no route"
        raise httpx.ConnectError(message, request=request)

    with pytest.raises(CollectionQueueError, match="403"):
        asyncio.run(refused.enqueue(JOB_ID, delay_seconds=0))
    with pytest.raises(CollectionQueueError):
        asyncio.run(_queue(unreachable).enqueue(JOB_ID, delay_seconds=0))


def test_default_credentials_supply_the_access_token(monkeypatch):
    refreshed = []

    class FakeCredentials:
        valid = False
        token = None

        def refresh(self, _request):
            refreshed.append(True)
            self.valid = True
            self.token = "fresh-token"

    monkeypatch.setattr(
        jobs_module.google.auth,
        "default",
        lambda scopes: (FakeCredentials(), "policyengine-staging"),
    )
    tokens = jobs_module.GoogleAccessTokens()

    assert asyncio.run(tokens()) == "fresh-token"
    assert asyncio.run(tokens()) == "fresh-token"
    assert refreshed == [True]
