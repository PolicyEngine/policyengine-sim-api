"""Google OIDC verification for the Cloud Tasks and Cloud Scheduler routes."""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from policyengine_simulation_entry.google_oidc import (
    GoogleOidcVerifier,
    InternalCallerRejectedError,
)

AUDIENCE = "https://policyengine-simulation-entry-staging-abc-uc.a.run.app"
INVOKER = "simulation-jobs-invoker@policyengine-staging.iam.gserviceaccount.com"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeGoogleKeys:
    """Return the test signing key, as PyJWKClient returns Google's."""

    def get_signing_key_from_jwt(self, _token):
        return SimpleNamespace(key=SIGNING_KEY.public_key())


def _token(key=SIGNING_KEY, algorithm="RS256", **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": INVOKER,
        "email_verified": True,
        "iat": now,
        "exp": now + 300,
        "sub": "1234567890",
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    signing_key = key if algorithm == "RS256" else "shared-secret-" + "x" * 32
    return jwt.encode(claims, signing_key, algorithm=algorithm)


def _verifier():
    return GoogleOidcVerifier(
        audience=AUDIENCE,
        service_account_email=INVOKER,
        jwks_client=FakeGoogleKeys(),
    )


@pytest.mark.parametrize(
    "issuer", ["https://accounts.google.com", "accounts.google.com"]
)
def test_a_token_from_the_invoker_account_is_accepted(issuer):
    _verifier().verify(_token(iss=issuer))


@pytest.mark.parametrize(
    "token",
    [
        _token(aud="https://another-service.a.run.app"),
        _token(iss="https://evil.example"),
        _token(email="someone-else@policyengine-staging.iam.gserviceaccount.com"),
        _token(email_verified=False),
        _token(email=None),
        _token(exp=int(time.time()) - 60),
        _token(key=OTHER_KEY),
        _token(algorithm="HS256"),
        "not-a-token",
    ],
    ids=[
        "audience",
        "issuer",
        "account",
        "unverified-email",
        "no-email",
        "expired",
        "wrong-key",
        "symmetric",
        "malformed",
    ],
)
def test_every_other_token_is_rejected(token):
    with pytest.raises(InternalCallerRejectedError):
        _verifier().verify(token)


def test_the_verifier_reads_google_signing_keys_by_default():
    verifier = GoogleOidcVerifier(audience=AUDIENCE, service_account_email=INVOKER)

    assert verifier.jwks_client.uri == "https://www.googleapis.com/oauth2/v3/certs"
