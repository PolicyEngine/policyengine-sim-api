"""Check that a request comes from sim's call-report Modal function.

Modal gives every function container an OIDC identity token
(``MODAL_IDENTITY_TOKEN``), signed with RS256 by ``MODAL_OIDC_ISSUER``, whose
claims name the container's workspace, environment, app and function. A
call-ended report carries it as a bearer token. The entrypoint accepts it
only when Modal's published key verifies its signature, its issuer, audience
and expiry are valid, and it names the expected workspace and environment, a
v2 app (``V2_APPLICATION_PREFIX``) and the call-report function
(``CALL_REPORT_FUNCTION_NAME``). A token of any other Modal function, such as
a worker running a user's policy, is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import jwt
from policyengine_simulation_contract.call_reports import CALL_REPORT_FUNCTION_NAME
from policyengine_simulation_contract.stage12_manifest import V2_APPLICATION_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

MODAL_OIDC_ISSUER = "https://oidc.modal.com"
MODAL_OIDC_AUDIENCE = "oidc.modal.com"
MODAL_JWKS_URL = "https://oidc.modal.com/.well-known/jwks.json"
# Modal lists RS256 as its only signing algorithm.
MODAL_SIGNING_ALGORITHMS = ("RS256",)
# Tolerated clock difference between Modal and this instance.
CLOCK_LEEWAY_SECONDS = 30


class ModalIdentityError(Exception):
    """The token does not prove that the request comes from the expected caller."""


class ModalKeysUnavailableError(Exception):
    """Modal's published keys could not be fetched, so no token can be checked."""


class SigningKeySource(Protocol):
    """Where the key that signed a token is found (``jwt.PyJWKClient``)."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        """Return the published key that signed ``token``."""
        ...


@dataclass(frozen=True)
class ExpectedModalCaller:
    """The only Modal caller whose reports are accepted."""

    workspace_id: str
    environment_name: str
    app_name_prefix: str = V2_APPLICATION_PREFIX
    function_name: str = CALL_REPORT_FUNCTION_NAME

    def __post_init__(self) -> None:
        """Refuse an expectation that any workspace or environment would meet."""
        if not self.workspace_id:
            msg = "the expected Modal workspace is not set"
            raise ValueError(msg)
        if not self.environment_name:
            msg = "the expected Modal environment is not set"
            raise ValueError(msg)


@dataclass(frozen=True)
class ModalIdentity:
    """The verified claims of a Modal container's identity token."""

    app_name: str
    function_name: str
    container_id: str


def _claim(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    return value if isinstance(value, str) else ""


class ModalIdentityVerifier:
    """Verify Modal identity tokens against Modal's published keys."""

    def __init__(
        self,
        expected: ExpectedModalCaller,
        *,
        signing_keys: SigningKeySource | None = None,
    ) -> None:
        """Expect ``expected``; fetch keys from ``MODAL_JWKS_URL`` unless given."""
        self.expected = expected
        self.signing_keys: SigningKeySource = signing_keys or jwt.PyJWKClient(
            MODAL_JWKS_URL
        )

    def verify(self, token: str) -> ModalIdentity:
        """Return the token's identity, or raise ModalIdentityError.

        Raise ModalKeysUnavailableError when Modal's keys cannot be fetched.
        Fetching them makes a network request the first time and whenever
        the key cache expires, so call this in a worker thread.
        """
        try:
            signing_key = self.signing_keys.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as error:
            msg = "Modal's signing keys could not be fetched"
            raise ModalKeysUnavailableError(msg) from error
        except jwt.PyJWTError as error:
            msg = f"the token names no key Modal publishes: {type(error).__name__}"
            raise ModalIdentityError(msg) from error
        # PyJWKClient wraps only URLError and TimeoutError; a key set that
        # arrives truncated, reset or not as JSON raises something else, and
        # is no more the caller's fault.
        except Exception as error:
            msg = f"Modal's signing keys could not be read: {type(error).__name__}"
            raise ModalKeysUnavailableError(msg) from error
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(MODAL_SIGNING_ALGORITHMS),
                audience=MODAL_OIDC_AUDIENCE,
                issuer=MODAL_OIDC_ISSUER,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            msg = f"the token is not valid: {type(error).__name__}"
            raise ModalIdentityError(msg) from error
        return self._caller(claims)

    def _caller(self, claims: Mapping[str, object]) -> ModalIdentity:
        expected = self.expected
        if _claim(claims, "workspace_id") != expected.workspace_id:
            msg = "the token names another Modal workspace"
            raise ModalIdentityError(msg)
        if _claim(claims, "environment_name") != expected.environment_name:
            msg = "the token names another Modal environment"
            raise ModalIdentityError(msg)
        app_name = _claim(claims, "app_name")
        if not app_name.startswith(expected.app_name_prefix):
            msg = "the token names another Modal app"
            raise ModalIdentityError(msg)
        function_name = _claim(claims, "function_name")
        if function_name != expected.function_name:
            msg = "the token names another Modal function"
            raise ModalIdentityError(msg)
        return ModalIdentity(
            app_name=app_name,
            function_name=function_name,
            container_id=_claim(claims, "container_id"),
        )
