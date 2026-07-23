from __future__ import annotations

import json

import httpx
import pytest

from policyengine_simulation_api.backend import (
    BackendUnavailable,
    OldGatewayBackend,
)

from conftest import make_settings


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
                "X-Request-ID": "upstream-request",
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
    assert upstream.headers["x-request-id"] == "caller-request"
    assert json.loads(result.content) == {
        "job_id": "fc-123",
        "status": "submitted",
    }
    assert result.headers["retry-after"] == "2"
    assert "set-cookie" not in result.headers


@pytest.mark.asyncio
async def test_backend_refreshes_token_once_after_401():
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
            return httpx.Response(401)
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
        raise httpx.ConnectError("unavailable", request=request)

    backend = OldGatewayBackend(
        make_settings(),
        transport=httpx.MockTransport(handler),
    )
    await backend.start()
    try:
        with pytest.raises(BackendUnavailable):
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
            raise httpx.ConnectError("unavailable", request=request)
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
