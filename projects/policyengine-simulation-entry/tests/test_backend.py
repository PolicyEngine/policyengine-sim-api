from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import (
    MODAL_STOPPED_CONTAINER_BODY,
    MODAL_STOPPED_CONTAINER_REPLY,
    RUNNING_JOB_REPLY,
    GatewayReply,
    RecordedWaits,
    gateway_transport_replying_in_turn,
    make_settings,
)
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_entry.backend import (
    BackendAuthenticationError,
    BackendInterruptedError,
    BackendResponse,
    BackendTimeoutError,
    BackendUnavailableError,
    InterruptedReadRetry,
    OldGatewayBackend,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_payload",
    [
        {},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "service-token", "expires_in": 0},
    ],
    ids=["missing-fields", "empty-token", "non-positive-expiry"],
)
async def test_backend_rejects_an_invalid_service_token_schema(token_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        return httpx.Response(200, json=token_payload)

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        with pytest.raises(BackendAuthenticationError):
            await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_backend_uses_own_token_and_preserves_safe_response_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "service-token", "expires_in": 3600},
            )
        return httpx.Response(
            202,
            json={"job_id": "fc-123", "status": "submitted"},
            headers={
                "Retry-After": "2",
                "Set-Cookie": "must-not-pass",
                REQUEST_ID_HEADER: "upstream-request",
            },
        )

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        result = await backend.request(
            "POST",
            "/simulate/economy/comparison",
            json_body={"country": "us"},
            request_id="caller-request",
        )
    finally:
        await backend.close()

    upstream = requests[-1]
    assert upstream.headers["authorization"] == "Bearer service-token"
    assert upstream.headers[REQUEST_ID_HEADER] == "caller-request"
    assert "x-request-id" not in upstream.headers
    assert json.loads(result.content) == {
        "job_id": "fc-123",
        "status": "submitted",
    }
    assert result.headers["retry-after"] == "2"
    assert result.headers[REQUEST_ID_HEADER] == "upstream-request"
    assert "set-cookie" not in result.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection_status", [401, 403])
async def test_backend_refreshes_token_once_after_gateway_auth_rejection(
    rejection_status,
):
    token_fetches = 0
    gateway_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_fetches, gateway_calls
        if request.url.path == "/oauth/token":
            token_fetches += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_fetches}",
                    "expires_in": 3600,
                },
            )
        gateway_calls += 1
        if gateway_calls == 1:
            return httpx.Response(rejection_status)
        return httpx.Response(200, json={"status": "complete"})

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        result = await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert result.status_code == 200
    assert token_fetches == 2
    assert gateway_calls == 2


@pytest.mark.asyncio
async def test_repeated_gateway_auth_rejection_is_backend_unavailable():
    token_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_fetches
        if request.url.path == "/oauth/token":
            token_fetches += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{token_fetches}",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(403)

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        with pytest.raises(BackendAuthenticationError):
            await backend.request("POST", "/simulate/economy/comparison")
    finally:
        await backend.close()

    assert token_fetches == 2


@pytest.mark.asyncio
async def test_short_lived_backend_token_is_never_cached_past_expiry():
    token_fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_fetches
        if request.url.path == "/oauth/token":
            token_fetches += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{token_fetches}", "expires_in": 1},
            )
        return httpx.Response(200, json={"status": "complete"})

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        await backend.request("GET", "/jobs/fc-123")
        await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert token_fetches == 2


@pytest.mark.asyncio
async def test_post_connection_error_is_not_retried():
    gateway_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        gateway_attempts += 1
        msg = "unavailable"
        raise httpx.ConnectError(msg, request=request)

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        with pytest.raises(BackendUnavailableError):
            await backend.request(
                "POST",
                "/simulate/economy/comparison",
                json_body={"country": "us"},
            )
    finally:
        await backend.close()

    assert gateway_attempts == 1


@pytest.mark.asyncio
async def test_get_connection_error_is_retried_once():
    gateway_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
            )
        gateway_attempts += 1
        if gateway_attempts == 1:
            msg = "unavailable"
            raise httpx.ConnectError(msg, request=request)
        return httpx.Response(200, json={"status": "complete"})

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        result = await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert result.status_code == 200
    assert gateway_attempts == 2


# The Public API's timeout for each call it makes to the Simulation Entrypoint
# (``SimulationEntrypointClient`` in policyengine-api).
PUBLIC_API_ENTRY_TIMEOUT_SECONDS = 30.0

FAILED_JOB_REPLY = GatewayReply(
    500,
    json.dumps({"status": "failed", "error": "Simulation failed"}).encode(),
    "application/json",
)
GATEWAY_UNHANDLED_ERROR_REPLY = GatewayReply(
    500, b"Internal Server Error", "text/plain; charset=utf-8"
)


async def _read_job_with_replies(
    replies: list[GatewayReply | Exception],
    *,
    gateway_requests: list[httpx.Request] | None = None,
    interrupted_read_retry: InterruptedReadRetry | None = None,
) -> BackendResponse:
    """Read a job's status from a backend whose gateway gives ``replies`` in turn."""
    backend = OldGatewayBackend(
        make_settings(),
        transport=gateway_transport_replying_in_turn(
            replies, gateway_requests=gateway_requests
        ),
        interrupted_read_retry=interrupted_read_retry,
    )
    await backend.start()
    try:
        return await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_a_read_modal_answers_for_a_stopped_gateway_is_sent_again():
    """PolicyEngine/policyengine-api#4216: Modal preempted the gateway in a job poll.

    Another gateway container answers the same read, and that answer is
    relayed.
    """
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()

    result = await _read_job_with_replies(
        [MODAL_STOPPED_CONTAINER_REPLY, RUNNING_JOB_REPLY],
        gateway_requests=gateway_requests,
        interrupted_read_retry=waits.retry(),
    )

    assert result.status_code == 202
    assert result.content == RUNNING_JOB_REPLY.content
    assert result.interrupted_attempts == 1
    assert [request.method for request in gateway_requests] == ["GET", "GET"]
    assert {request.url.path for request in gateway_requests} == {"/jobs/fc-123"}
    assert waits.seconds == [0.5]


@pytest.mark.asyncio
async def test_a_read_modal_answers_for_a_stopped_gateway_is_interrupted():
    """Run 42894301943: Modal preempted the gateway during a job poll.

    When Modal answers every attempt, the read ends interrupted.
    """
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()

    with pytest.raises(BackendInterruptedError) as interrupted:
        await _read_job_with_replies(
            [MODAL_STOPPED_CONTAINER_REPLY],
            gateway_requests=gateway_requests,
            interrupted_read_retry=waits.retry(),
        )

    assert interrupted.value.interrupted_attempts == 3
    assert len(gateway_requests) == 3
    assert waits.seconds == [0.5, 1.0]


@pytest.mark.asyncio
async def test_retrying_an_interrupted_read_waits_less_than_its_callers_do():
    waits = RecordedWaits()
    retry = waits.retry()

    with pytest.raises(BackendInterruptedError):
        await _read_job_with_replies(
            [MODAL_STOPPED_CONTAINER_REPLY], interrupted_read_retry=retry
        )

    assert len(waits.seconds) == len(retry.waits_seconds)
    assert sum(waits.seconds) <= retry.window_seconds
    assert retry.window_seconds <= make_settings().request_timeout_seconds / 5
    assert retry.window_seconds <= PUBLIC_API_ENTRY_TIMEOUT_SECONDS / 5


@pytest.mark.asyncio
async def test_no_attempt_of_an_interrupted_read_starts_after_its_window():
    """No attempt of a read starts more than 5 seconds after the read began.

    Each gateway reply here takes 3 seconds, so a third attempt would start
    7.5 seconds after the read began.
    """
    elapsed_seconds = 0.0
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()
    replies = gateway_transport_replying_in_turn(
        [MODAL_STOPPED_CONTAINER_REPLY], gateway_requests=gateway_requests
    )

    class SlowGateway(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal elapsed_seconds
            if request.url.path != "/oauth/token":
                elapsed_seconds += 3.0
            return await replies.handle_async_request(request)

    async def sleep(seconds: float) -> None:
        nonlocal elapsed_seconds
        await waits.sleep(seconds)
        elapsed_seconds += seconds

    backend = OldGatewayBackend(
        make_settings(),
        transport=SlowGateway(),
        interrupted_read_retry=InterruptedReadRetry(
            sleep=sleep, clock=lambda: elapsed_seconds
        ),
    )
    await backend.start()
    try:
        with pytest.raises(BackendInterruptedError) as interrupted:
            await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert interrupted.value.interrupted_attempts == 2
    assert len(gateway_requests) == 2
    assert waits.seconds == [0.5]


@pytest.mark.asyncio
async def test_no_attempt_of_an_interrupted_read_starts_after_its_read_timeout():
    """A shorter gateway read timeout than the window ends the retries first.

    The first attempt here takes 2 seconds, so the second would start 2.5
    seconds into a read whose timeout is 2.2 seconds.
    """
    elapsed_seconds = 0.0
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()
    replies = gateway_transport_replying_in_turn(
        [MODAL_STOPPED_CONTAINER_REPLY], gateway_requests=gateway_requests
    )

    class SlowGateway(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal elapsed_seconds
            if request.url.path != "/oauth/token":
                elapsed_seconds += 2.0
            return await replies.handle_async_request(request)

    backend = OldGatewayBackend(
        make_settings(request_timeout_seconds=2.2),
        transport=SlowGateway(),
        interrupted_read_retry=InterruptedReadRetry(
            sleep=waits.sleep, clock=lambda: elapsed_seconds
        ),
    )
    await backend.start()
    try:
        with pytest.raises(BackendInterruptedError) as interrupted:
            await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert interrupted.value.interrupted_attempts == 1
    assert len(gateway_requests) == 1
    assert waits.seconds == []


@pytest.mark.asyncio
async def test_a_read_sent_again_ends_within_the_gateway_read_timeout():
    """A read sent again never takes longer than the gateway read timeout.

    The first attempt here takes 4.4 seconds before Modal answers, so the
    second starts 4.9 seconds into the read and may take only the 0.3 seconds
    left of a 5.2-second timeout, though the gateway would take 5 seconds.
    """
    elapsed_seconds = 0.0
    gateway_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal elapsed_seconds, gateway_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "service-token", "expires_in": 3600}
            )
        gateway_attempts += 1
        if gateway_attempts == 1:
            elapsed_seconds += 4.4
            return httpx.Response(500, content=MODAL_STOPPED_CONTAINER_BODY)
        await asyncio.sleep(5)
        return httpx.Response(202, content=RUNNING_JOB_REPLY.content)

    async def sleep(seconds: float) -> None:
        nonlocal elapsed_seconds
        elapsed_seconds += seconds

    backend = OldGatewayBackend(
        make_settings(request_timeout_seconds=5.2),
        transport=httpx.MockTransport(handler),
        interrupted_read_retry=InterruptedReadRetry(
            sleep=sleep, clock=lambda: elapsed_seconds
        ),
    )
    await backend.start()
    started = time.monotonic()
    try:
        with pytest.raises(BackendTimeoutError) as timed_out:
            await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert time.monotonic() - started < 2
    assert timed_out.value.interrupted_attempts == 1
    assert gateway_attempts == 2


@pytest.mark.asyncio
async def test_a_third_attempt_ends_within_the_gateway_read_timeout():
    """The read timeout counts every earlier attempt and wait, not one attempt.

    Two attempts Modal answers and their waits take 1.7 seconds, so the third
    may take only the 0.3 seconds left of a 2-second timeout, though the
    gateway would take 5 seconds.
    """
    elapsed_seconds = 0.0
    gateway_attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal elapsed_seconds, gateway_attempts
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "service-token", "expires_in": 3600}
            )
        gateway_attempts += 1
        if gateway_attempts < 3:
            elapsed_seconds += 0.1
            return httpx.Response(500, content=MODAL_STOPPED_CONTAINER_BODY)
        await asyncio.sleep(5)
        return httpx.Response(202, content=RUNNING_JOB_REPLY.content)

    async def sleep(seconds: float) -> None:
        nonlocal elapsed_seconds
        elapsed_seconds += seconds

    backend = OldGatewayBackend(
        make_settings(request_timeout_seconds=2.0),
        transport=httpx.MockTransport(handler),
        interrupted_read_retry=InterruptedReadRetry(
            sleep=sleep, clock=lambda: elapsed_seconds
        ),
    )
    await backend.start()
    started = time.monotonic()
    try:
        with pytest.raises(BackendTimeoutError) as timed_out:
            await backend.request("GET", "/jobs/fc-123")
    finally:
        await backend.close()

    assert time.monotonic() - started < 1.5
    assert timed_out.value.interrupted_attempts == 2
    assert gateway_attempts == 3


@pytest.mark.asyncio
async def test_an_interrupted_read_waits_with_asyncio_sleep_by_default():
    gateway_requests: list[httpx.Request] = []

    result = await _read_job_with_replies(
        [MODAL_STOPPED_CONTAINER_REPLY, RUNNING_JOB_REPLY],
        gateway_requests=gateway_requests,
        interrupted_read_retry=InterruptedReadRetry(waits_seconds=(0.01,)),
    )

    assert result.status_code == 202
    assert len(gateway_requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replies",
    [
        [FAILED_JOB_REPLY],
        [GATEWAY_UNHANDLED_ERROR_REPLY],
        [MODAL_STOPPED_CONTAINER_REPLY, FAILED_JOB_REPLY],
    ],
    ids=["failed-job", "gateway-unhandled-error", "failed-job-after-modal-500"],
)
async def test_a_500_from_the_gateway_itself_is_returned_without_another_attempt(
    replies,
):
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()

    result = await _read_job_with_replies(
        replies,
        gateway_requests=gateway_requests,
        interrupted_read_retry=waits.retry(),
    )

    assert result.status_code == 500
    assert result.content == replies[-1].content
    assert len(gateway_requests) == len(replies)
    assert result.interrupted_attempts == len(replies) - 1
    assert len(waits.seconds) == len(replies) - 1


@pytest.mark.asyncio
async def test_a_submission_modal_answers_for_a_stopped_gateway_is_returned():
    """The job may have started, so the submission is never sent again."""
    gateway_requests: list[httpx.Request] = []
    waits = RecordedWaits()

    backend = OldGatewayBackend(
        make_settings(),
        transport=gateway_transport_replying_in_turn(
            [MODAL_STOPPED_CONTAINER_REPLY, RUNNING_JOB_REPLY],
            gateway_requests=gateway_requests,
        ),
        interrupted_read_retry=waits.retry(),
    )
    await backend.start()
    try:
        result = await backend.request(
            "POST",
            "/simulate/economy/comparison",
            json_body={"country": "us"},
        )
    finally:
        await backend.close()

    assert result.status_code == 500
    assert result.content == MODAL_STOPPED_CONTAINER_BODY
    assert result.interrupted_attempts == 0
    assert len(gateway_requests) == 1
    assert waits.seconds == []


@pytest.mark.asyncio
async def test_a_timeout_after_an_interrupted_read_counts_the_interrupted_attempt():
    waits = RecordedWaits()

    with pytest.raises(BackendTimeoutError) as timed_out:
        await _read_job_with_replies(
            [MODAL_STOPPED_CONTAINER_REPLY, httpx.ReadTimeout("gateway read")],
            interrupted_read_retry=waits.retry(),
        )

    assert timed_out.value.interrupted_attempts == 1
    assert waits.seconds == [0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health_status", "expected"),
    [(200, True), (204, False), (503, False)],
)
async def test_readiness_requires_a_200_health_response(health_status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "service-token", "expires_in": 3600},
            )
        assert request.method == "GET"
        assert request.url.path == "/health"
        return httpx.Response(health_status, json={"status": "healthy"})

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        assert await backend.ready() is expected
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_readiness_sends_a_health_read_modal_answers_again():
    """A health read is a GET, so it is sent again like a job read.

    The WARNING event for a read sent again is recorded only for the routes
    that forward to the gateway; ``/ready`` records none.
    """
    gateway_requests: list[httpx.Request] = []
    backend = OldGatewayBackend(
        make_settings(),
        transport=gateway_transport_replying_in_turn(
            [
                MODAL_STOPPED_CONTAINER_REPLY,
                GatewayReply(200, b'{"status": "healthy"}', "application/json"),
            ],
            gateway_requests=gateway_requests,
        ),
        interrupted_read_retry=RecordedWaits().retry(),
    )
    await backend.start()
    try:
        assert await backend.ready() is True
    finally:
        await backend.close()

    assert [request.url.path for request in gateway_requests] == [
        "/health",
        "/health",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["unreachable", "timed-out"],
)
async def test_readiness_is_false_when_the_gateway_cannot_answer(failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "service-token", "expires_in": 3600},
            )
        raise failure

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        assert await backend.ready() is False
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_readiness_is_false_before_the_backend_starts():
    assert await OldGatewayBackend(make_settings()).ready() is False
