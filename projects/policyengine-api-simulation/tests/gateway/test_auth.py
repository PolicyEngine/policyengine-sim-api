"""Tests for gateway authentication middleware."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fixtures.gateway.shared import create_gateway_app
from src.modal.gateway import auth as auth_module


GATED_REQUESTS = [
    (
        "post",
        "/simulate/economy/comparison",
        {"country": "us", "scope": "macro", "reform": {}},
    ),
    (
        "post",
        "/simulate/economy/budget-window",
        {
            "country": "us",
            "region": "us",
            "scope": "macro",
            "reform": {},
            "start_year": "2026",
            "window_size": 3,
        },
    ),
    ("get", "/jobs/some-job-id", None),
    ("get", "/budget-window-jobs/some-job-id", None),
]


@pytest.fixture
def unauthenticated_client(monkeypatch) -> TestClient:
    """A client where the real auth dependency is active but no token is
    attached. The underlying JWTDecoder is stubbed to preserve the 403
    contract without making a live JWKS fetch."""

    class FailingDecoder:
        def __call__(self, token):
            from fastapi import HTTPException, status

            if token is None:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    monkeypatch.setattr(auth_module, "_get_decoder", lambda: FailingDecoder())
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, "1")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud")

    return TestClient(create_gateway_app(authenticate=False))


@pytest.mark.parametrize("method,path,body", GATED_REQUESTS)
def test__given_no_bearer_token__then_gated_endpoint_returns_403(
    unauthenticated_client, method, path, body
):
    """Missing bearer tokens should be rejected on all private endpoints."""

    if method == "post":
        response = unauthenticated_client.post(path, json=body)
    else:
        response = unauthenticated_client.get(path)

    assert response.status_code == 403


def test__given_auth_disabled_env__then_dependency_returns_none(monkeypatch):
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
    assert auth_module.require_auth(token=None) is None


def test__given_auth_not_configured_and_not_required__then_dependency_allows(
    monkeypatch,
):
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

    assert auth_module.require_auth(token=None) is None


def test__given_auth_configured_but_not_required__then_dependency_allows(
    monkeypatch,
):
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud")

    assert auth_module.require_auth(token=None) is None


def test__given_auth_required_and_misconfigured__then_dependency_raises_503(
    monkeypatch,
):
    from fastapi import HTTPException

    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, "1")
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        auth_module.require_auth(token=None)

    assert exc_info.value.status_code == 503


def test__given_partial_auth_config__then_dependency_raises_503(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")
    monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

    with pytest.raises(HTTPException) as exc_info:
        auth_module.require_auth(token=None)

    assert exc_info.value.status_code == 503


def test__given_health_endpoint__then_auth_not_required(monkeypatch):
    """Health/ping/versions endpoints remain public by design."""

    from fixtures.gateway.shared import create_gateway_app

    client = TestClient(create_gateway_app(authenticate=False))
    response = client.get("/health")
    assert response.status_code == 200


def test__given_same_env__then_decoder_is_cached(monkeypatch):
    """Calling ``_get_decoder`` repeatedly must return the same instance so
    the wrapped ``PyJWKClient`` JWKS cache is reused across requests.
    Rebuilding the decoder per request defeats the cache (#458 review)."""

    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud-caching")

    auth_module.reset_decoder_cache()
    first = auth_module._get_decoder()
    second = auth_module._get_decoder()

    assert first is second


def test__given_rotated_audience__then_cache_returns_new_decoder(monkeypatch):
    """Cache is keyed on issuer+audience so rotating the audience yields a
    fresh decoder without polluting the previous one."""

    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")

    auth_module.reset_decoder_cache()

    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud-first")
    first = auth_module._get_decoder()

    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud-second")
    second = auth_module._get_decoder()

    assert first is not second
    assert first.audience == "aud-first"
    assert second.audience == "aud-second"


def test__given_repeated_requests__then_decoder_not_reinstantiated(monkeypatch):
    """Smoke test: hitting a gated endpoint many times must not rebuild the
    decoder. We spy on ``JWTDecoder.__init__`` and assert it runs at most
    once across many requests."""

    from fixtures.gateway.shared import create_gateway_app
    from policyengine_fastapi.auth import jwt_decoder as jwt_decoder_module

    monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, "1")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/")
    monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud-repeat")

    auth_module.reset_decoder_cache()

    init_calls = {"count": 0}
    original_init = jwt_decoder_module.JWTDecoder.__init__

    def _counting_init(self, *args, **kwargs):
        init_calls["count"] += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(jwt_decoder_module.JWTDecoder, "__init__", _counting_init)

    client = TestClient(create_gateway_app(authenticate=False))

    for _ in range(5):
        response = client.get("/jobs/some-id")
        assert response.status_code == 403

    assert init_calls["count"] == 1, (
        f"Expected exactly one JWTDecoder instantiation across 5 requests, "
        f"got {init_calls['count']}"
    )


def test__given_dependency_override__then_gated_endpoint_returns_200(
    mock_modal, client: TestClient
):
    """Positive-path auth test: when ``app.dependency_overrides`` bypasses
    ``require_auth`` (as real tests do), the gated endpoint returns 200 with
    the expected payload. This guards the override path that other tests
    rely on — if the override stops working, every gated-endpoint test would
    start returning 403 instead of exercising real logic."""

    mock_modal["dicts"]["simulation-api-us-versions"] = {
        "latest": "1.500.0",
        "1.500.0": "policyengine-simulation-py4-10-0",
    }

    response = client.post(
        "/simulate/economy/comparison",
        json={"country": "us", "scope": "macro", "reform": {}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == "mock-job-id-123"
    assert body["country"] == "us"
    assert body["version"] == "4.10.0"


class TestProductionAuthGuard:
    """Tests for ``enforce_production_auth_guard`` — the startup-time check
    that prevents ``GATEWAY_AUTH_DISABLED`` from slipping into production."""

    def test__given_auth_enabled__then_guard_noops(self, monkeypatch):
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
        monkeypatch.delenv(auth_module.MODAL_ENVIRONMENT_ENV, raising=False)

        # Must not raise even without any prod env configured.
        auth_module.enforce_production_auth_guard()

    def test__given_disabled_and_modal_env_missing__then_refuses(self, monkeypatch):
        """Unset MODAL_ENVIRONMENT is treated as production: refuse."""
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.delenv(auth_module.MODAL_ENVIRONMENT_ENV, raising=False)
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV,
            auth_module.GATEWAY_AUTH_DISABLED_ACK_VALUE,
        )

        with pytest.raises(auth_module.AuthDisabledInProductionError):
            auth_module.enforce_production_auth_guard()

    @pytest.mark.parametrize("prod_env", ["main", "prod", "production", "PROD"])
    def test__given_disabled_and_prod_modal_env__then_refuses(
        self, monkeypatch, prod_env
    ):
        """Named production environments reject the bypass even with ACK."""
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.setenv(auth_module.MODAL_ENVIRONMENT_ENV, prod_env)
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV,
            auth_module.GATEWAY_AUTH_DISABLED_ACK_VALUE,
        )

        with pytest.raises(auth_module.AuthDisabledInProductionError):
            auth_module.enforce_production_auth_guard()

    def test__given_disabled_in_dev_without_ack__then_refuses(self, monkeypatch):
        """A single env var (``GATEWAY_AUTH_DISABLED=1``) is not enough."""
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.setenv(auth_module.MODAL_ENVIRONMENT_ENV, "dev")
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV, raising=False)

        with pytest.raises(auth_module.AuthDisabledWithoutAckError):
            auth_module.enforce_production_auth_guard()

    def test__given_disabled_in_dev_with_wrong_ack__then_refuses(self, monkeypatch):
        """ACK must exactly match the magic string — truthy is not enough."""
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.setenv(auth_module.MODAL_ENVIRONMENT_ENV, "dev")
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV, "yes")

        with pytest.raises(auth_module.AuthDisabledWithoutAckError):
            auth_module.enforce_production_auth_guard()

    def test__given_disabled_in_dev_with_correct_ack__then_allows_and_logs(
        self, monkeypatch, caplog
    ):
        """The bypass is permitted but emits a CRITICAL log for audit."""
        import logging

        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.setenv(auth_module.MODAL_ENVIRONMENT_ENV, "dev")
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV,
            auth_module.GATEWAY_AUTH_DISABLED_ACK_VALUE,
        )

        with caplog.at_level(logging.CRITICAL, logger=auth_module.logger.name):
            auth_module.enforce_production_auth_guard()

        assert any(
            "GATEWAY AUTH IS DISABLED" in record.message for record in caplog.records
        ), f"Expected critical auth-disabled banner, got {caplog.records!r}"

    def test__given_bypass_active__then_logfire_event_gated_on_configuration(
        self, monkeypatch
    ):
        """The legacy Logfire audit event fires only when configure_logfire
        actually ran with a token — not based on logfire's send_to_logfire
        flag, which is True even on an unconfigured instance."""
        import sys

        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.setenv(auth_module.MODAL_ENVIRONMENT_ENV, "dev")
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_DISABLED_ACK_ENV,
            auth_module.GATEWAY_AUTH_DISABLED_ACK_VALUE,
        )

        class _FakeLogfire:
            def __init__(self):
                self.calls = []

            def error(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        fake_logfire = _FakeLogfire()
        monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

        monkeypatch.setattr(auth_module, "logfire_is_configured", lambda: False)
        auth_module.enforce_production_auth_guard()
        assert fake_logfire.calls == []

        monkeypatch.setattr(auth_module, "logfire_is_configured", lambda: True)
        auth_module.enforce_production_auth_guard()
        assert len(fake_logfire.calls) == 1
        args, kwargs = fake_logfire.calls[0]
        assert args == ("gateway_auth_disabled_bypass_active",)
        assert kwargs["modal_environment"] == "dev"


class TestAuthConfiguredGuard:
    """Startup guard for required-or-partial auth misconfiguration."""

    def test__given_auth_disabled__then_guard_noops(self, monkeypatch):
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, "1")
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

        auth_module.enforce_auth_configured_guard()

    def test__given_auth_optional_and_unset__then_guard_noops(self, monkeypatch):
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

        auth_module.enforce_auth_configured_guard()

    def test__given_partial_auth_config__then_guard_raises(self, monkeypatch):
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, raising=False)
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/"
        )
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

        with pytest.raises(auth_module.AuthMisconfiguredError):
            auth_module.enforce_auth_configured_guard()

    def test__given_required_and_missing__then_guard_raises(self, monkeypatch):
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, "1")
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_ISSUER_ENV, raising=False)
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, raising=False)

        with pytest.raises(auth_module.AuthMisconfiguredError):
            auth_module.enforce_auth_configured_guard()

    def test__given_required_and_configured__then_guard_noops(self, monkeypatch):
        monkeypatch.delenv(auth_module.GATEWAY_AUTH_DISABLED_ENV, raising=False)
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_REQUIRED_ENV, "1")
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/"
        )
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud")

        auth_module.enforce_auth_configured_guard()


class TestIssuerNormalization:
    """Issuer should be normalized to the trailing-slash Auth0 form."""

    def test__given_issuer_without_slash__then_decoder_receives_normalized_value(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example"
        )
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud")
        auth_module.reset_decoder_cache()

        captured = {}

        def fake_builder(issuer, audience):
            captured["issuer"] = issuer
            captured["audience"] = audience
            return object()

        monkeypatch.setattr(auth_module, "_build_decoder", fake_builder)

        auth_module._get_decoder()

        assert captured == {
            "issuer": "https://issuer.example/",
            "audience": "aud",
        }

    def test__given_issuer_with_slash__then_decoder_receives_unchanged_value(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            auth_module.GATEWAY_AUTH_ISSUER_ENV, "https://issuer.example/"
        )
        monkeypatch.setenv(auth_module.GATEWAY_AUTH_AUDIENCE_ENV, "aud")
        auth_module.reset_decoder_cache()

        captured = {}

        def fake_builder(issuer, audience):
            captured["issuer"] = issuer
            captured["audience"] = audience
            return object()

        monkeypatch.setattr(auth_module, "_build_decoder", fake_builder)

        auth_module._get_decoder()

        assert captured == {
            "issuer": "https://issuer.example/",
            "audience": "aud",
        }
