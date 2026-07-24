from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from policyengine_simulation_entry import auth as auth_module
from policyengine_simulation_entry.app import create_app

from conftest import FakeBackend, make_settings


@pytest.fixture
def signing_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _token(private_key, **overrides) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "api-v1",
        "iss": "https://issuer.example/",
        "aud": "simulation-entry",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _use_static_signing_key(monkeypatch, public_key) -> None:
    decoder = auth_module.JWTDecoder(
        issuer="https://issuer.example/",
        audience="simulation-entry",
    )
    decoder.jwks_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: decoder)


def test_missing_token_preserves_403_contract(monkeypatch):
    class Decoder:
        def __call__(self, token):
            from fastapi import HTTPException

            raise HTTPException(status_code=403)

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get("/jobs/job-1")

    assert result.status_code == 403


def test_public_routes_do_not_require_token(monkeypatch):
    class Decoder:
        def __call__(self, token):
            raise AssertionError("public route attempted authentication")

    monkeypatch.setattr(auth_module, "_decoder", lambda issuer, audience: Decoder())
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/versions").status_code == 200
        assert client.post("/ping", json={"value": 1}).status_code == 200


def test_valid_api_v1_token_can_poll(monkeypatch, signing_keys):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"Authorization": f"Bearer {_token(private_key)}"},
        )

    assert result.status_code == 200


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://wrong-issuer.example/"},
        {"aud": "wrong-audience"},
        {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
    ],
    ids=["wrong-issuer", "wrong-audience", "expired"],
)
def test_invalid_api_v1_token_claims_return_403(
    monkeypatch,
    signing_keys,
    claim_overrides,
):
    private_key, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={
                "Authorization": f"Bearer {_token(private_key, **claim_overrides)}"
            },
        )

    assert result.status_code == 403


def test_malformed_token_returns_403(monkeypatch, signing_keys):
    _, public_key = signing_keys
    _use_static_signing_key(monkeypatch, public_key)
    app = create_app(settings=make_settings(), backend=FakeBackend())

    with TestClient(app) as client:
        result = client.get(
            "/jobs/job-1",
            headers={"Authorization": "Bearer not-a-jwt"},
        )

    assert result.status_code == 403
