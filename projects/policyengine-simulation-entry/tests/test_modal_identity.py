"""Checking that a request comes from sim's call-report Modal function."""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from policyengine_simulation_entry.modal_identity import (
    MODAL_OIDC_AUDIENCE,
    MODAL_OIDC_ISSUER,
    ExpectedModalCaller,
    ModalIdentityError,
    ModalIdentityVerifier,
    ModalKeysUnavailableError,
)

WORKSPACE_ID = "ac-workspace0001"
EXPECTED = ExpectedModalCaller(workspace_id=WORKSPACE_ID, environment_name="staging")
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class _KeyFromJwks:
    """Stands in for jwt.PyJWKClient: the public key Modal publishes."""

    public_key: object
    fetches: int = 0

    def get_signing_key_from_jwt(self, _token: str):
        self.fetches += 1
        return jwt.PyJWK.from_dict(
            {
                **jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key, as_dict=True),
                "kid": "modal-key",
                "alg": "RS256",
            }
        )


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": MODAL_OIDC_ISSUER,
        "aud": MODAL_OIDC_AUDIENCE,
        "sub": "modal:workspace_id:ac-workspace0001:...",
        "iat": now,
        "exp": now + 3600,
        "workspace_id": WORKSPACE_ID,
        "environment_name": "staging",
        "app_name": "policyengine-simulation-v2-py6-10-0",
        "function_name": "report_call_ended",
        "container_id": "ta-container0001",
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _token(key=SIGNING_KEY, **overrides) -> str:
    return jwt.encode(
        _claims(**overrides), key, algorithm="RS256", headers={"kid": "modal-key"}
    )


def _verifier() -> ModalIdentityVerifier:
    return ModalIdentityVerifier(
        EXPECTED, signing_keys=_KeyFromJwks(SIGNING_KEY.public_key())
    )


def test_a_token_of_the_call_report_function_is_accepted() -> None:
    identity = _verifier().verify(_token())

    assert identity.app_name == "policyengine-simulation-v2-py6-10-0"
    assert identity.function_name == "report_call_ended"
    assert identity.container_id == "ta-container0001"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"workspace_id": "ac-someoneelse"}, "workspace"),
        ({"environment_name": "main"}, "environment"),
        ({"app_name": "policyengine-simulation-py6-10-0"}, "app"),
        ({"function_name": "coordinate_report"}, "function"),
        ({"workspace_id": None}, "workspace"),
        ({"function_name": None}, "function"),
    ],
    ids=[
        "other-workspace",
        "other-environment",
        "main-path-app",
        "other-function",
        "no-workspace",
        "no-function",
    ],
)
def test_a_token_of_any_other_modal_caller_is_refused(overrides, reason) -> None:
    with pytest.raises(ModalIdentityError, match=reason):
        _verifier().verify(_token(**overrides))


@pytest.mark.parametrize(
    "token",
    [
        _token(key=OTHER_KEY),
        _token(iss="https://oidc.example.com"),
        _token(aud="sim"),
        _token(exp=int(time.time()) - 3600),
        "not-a-token",
    ],
    ids=["wrong-key", "wrong-issuer", "wrong-audience", "expired", "malformed"],
)
def test_a_token_modal_did_not_sign_for_us_is_refused(token) -> None:
    with pytest.raises(ModalIdentityError):
        _verifier().verify(token)


def test_a_token_signed_with_another_algorithm_is_refused() -> None:
    token = jwt.encode(
        _claims(), "shared-secret-of-at-least-32-bytes!", algorithm="HS256"
    )

    with pytest.raises(ModalIdentityError):
        _verifier().verify(token)


def test_the_expected_caller_needs_a_workspace_and_an_environment() -> None:
    with pytest.raises(ValueError, match="workspace"):
        ExpectedModalCaller(workspace_id="", environment_name="staging")
    with pytest.raises(ValueError, match="environment"):
        ExpectedModalCaller(workspace_id=WORKSPACE_ID, environment_name="")


class _UnreachableJwks:
    def get_signing_key_from_jwt(self, _token: str):
        msg = "Fail to fetch data from the url"
        raise jwt.PyJWKClientConnectionError(msg)


def test_unreachable_modal_keys_are_not_a_refusal() -> None:
    """The sender should report again later, not give up on its report."""
    verifier = ModalIdentityVerifier(EXPECTED, signing_keys=_UnreachableJwks())

    with pytest.raises(ModalKeysUnavailableError):
        verifier.verify(_token())


class _BrokenJwks:
    """Modal's key set answered, but not with a key set PyJWT could read."""

    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def get_signing_key_from_jwt(self, _token: str):
        raise self.failure


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("Expecting value: line 1 column 1 (char 0)"),
        ConnectionResetError("reset while reading the key set"),
        OSError("truncated response"),
    ],
    ids=["not-json", "reset", "truncated"],
)
def test_any_failure_to_fetch_modal_keys_is_not_a_refusal(failure) -> None:
    verifier = ModalIdentityVerifier(EXPECTED, signing_keys=_BrokenJwks(failure))

    with pytest.raises(ModalKeysUnavailableError):
        verifier.verify(_token())


def test_a_token_naming_no_published_key_is_still_refused() -> None:
    verifier = ModalIdentityVerifier(
        EXPECTED,
        signing_keys=_BrokenJwks(jwt.PyJWKClientError("Unable to find a signing key")),
    )

    with pytest.raises(ModalIdentityError):
        verifier.verify(_token())
