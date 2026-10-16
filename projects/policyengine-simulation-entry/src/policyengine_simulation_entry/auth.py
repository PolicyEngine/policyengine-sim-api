"""Inbound bearer authentication and permission checks for protected routes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from policyengine_simulation_entry.schemas import CallerIdentity

if TYPE_CHECKING:
    from policyengine_observability import ObservabilityRuntime

    from policyengine_simulation_entry.config import Settings

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

type CallerPermission = Literal[
    "simulation:submit",
    "simulation:read",
    "stage12:operate",
]
type AuthenticationDependency = Callable[[], CallerIdentity | None]
type AuthorizationDependency = Callable[..., CallerIdentity | None]


class JWTDecoder:
    """Validate Auth0 RS256 access tokens without database-layer dependencies."""

    def __init__(self, issuer: str, audience: str) -> None:
        """Bind the issuer and audience and point a JWKS client at the issuer."""
        self.issuer = issuer
        self.audience = audience
        self.jwks_client = jwt.PyJWKClient(f"{issuer}.well-known/jwks.json")

    def __call__(
        self,
        token: HTTPAuthorizationCredentials | None,
        runtime: ObservabilityRuntime,
    ) -> CallerIdentity:
        """Return the caller identity from a verified token.

        Raise HTTP 403 for a missing or invalid token, recording the rejection
        reason as an observability event.
        """
        if token is None:
            runtime.event(
                "simulation_entry_auth_rejected",
                attributes={"reason": "missing_token"},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(
                token.credentials
            ).key
            return CallerIdentity.model_validate(
                jwt.decode(
                    token.credentials,
                    signing_key,
                    algorithms=["RS256"],
                    audience=self.audience,
                    issuer=self.issuer,
                )
            )
        except Exception as error:
            reason = type(error).__name__
            logger.info(
                "invalid_simulation_entry_bearer_token",
                extra={"error_type": reason},
            )
            runtime.event(
                "simulation_entry_auth_rejected",
                attributes={"reason": reason},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error


@lru_cache(maxsize=8)
def _decoder(issuer: str, audience: str) -> JWTDecoder:
    return JWTDecoder(issuer=issuer, audience=audience)


class CallerAuthenticator:
    """FastAPI dependency that preserves the gateway's JWT contract."""

    def __init__(self, settings: Settings, runtime: ObservabilityRuntime) -> None:
        """Keep the settings and runtime used to verify each request."""
        self.settings = settings
        self.runtime = runtime

    def __call__(
        self,
        token: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> CallerIdentity | None:
        """Return the verified caller, or None when auth is not required.

        Decoders are cached per issuer and audience, so the JWKS client is
        reused across requests.
        """
        if not self.settings.auth_required:
            return None
        return _decoder(
            self.settings.auth_issuer,
            self.settings.auth_audience,
        )(token, self.runtime)


def reset_decoder_cache() -> None:
    """Clear the decoder cache for tests and credential rotations."""
    _decoder.cache_clear()


def is_client_credentials(identity: CallerIdentity) -> bool:
    """Return whether the token was issued to a machine client."""
    return "client-credentials" in identity.grant_types or (
        identity.subject is not None and identity.subject.endswith("@clients")
    )


def granted_permissions(identity: CallerIdentity) -> frozenset[str]:
    """Return the permissions a token grants.

    The RBAC ``permissions`` claim counts for every token. The OAuth ``scope``
    claim counts only for client-credentials tokens, whose scopes come from the
    client's grant. Unless RBAC is enabled on the API, Auth0 copies whatever
    API scopes a user's application requested into a user token's ``scope``,
    so there it proves nothing.
    """
    scopes = (
        identity.scope.split()
        if identity.scope and is_client_credentials(identity)
        else []
    )
    return frozenset([*(identity.permissions or []), *scopes])


class CallerAuthorizer:
    """Build FastAPI dependencies that check one Auth0 permission per route.

    With enforcement disabled, a caller that lacks the permission is recorded
    and allowed, so missing grants can be found before enforcement begins.
    """

    def __init__(
        self,
        *,
        authenticate: AuthenticationDependency,
        enforce: bool,
        runtime: ObservabilityRuntime,
    ) -> None:
        """Authorize the callers that ``authenticate`` identifies.

        With ``enforce`` a caller that lacks a route's permission is rejected.
        Either way, ``runtime`` receives a ``simulation_entry_permission_missing``
        event for that caller.
        """
        self.authenticate = authenticate
        self.enforce = enforce
        self.runtime = runtime

    def require_permission(
        self,
        permission: CallerPermission,
    ) -> AuthorizationDependency:
        """Return a dependency that yields the caller holding ``permission``."""

        def authorized_caller(
            identity: CallerIdentity | None = Depends(self.authenticate),
        ) -> CallerIdentity | None:
            return self.authorize(identity, permission)

        return authorized_caller

    def authorize(
        self,
        identity: CallerIdentity | None,
        permission: CallerPermission,
    ) -> CallerIdentity | None:
        """Return ``identity`` when it may call a route that needs ``permission``.

        A caller without the permission is recorded, then rejected with a 403
        HTTPException when enforcement is on and allowed when it is off.
        """
        # No identity means caller authentication is disabled, which settings
        # validation permits only outside production. There are no claims to
        # check, so the request proceeds.
        if identity is None or permission in granted_permissions(identity):
            return identity
        self._record_missing_permission(identity, permission)
        if self.enforce:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return identity

    def _record_missing_permission(
        self,
        identity: CallerIdentity,
        permission: CallerPermission,
    ) -> None:
        attributes: dict[str, str | bool] = {
            "permission": permission,
            "enforced": self.enforce,
        }
        if identity.authorized_party:
            attributes["client_id"] = identity.authorized_party
        try:
            self.runtime.event(
                "simulation_entry_permission_missing",
                severity="WARNING",
                attributes=attributes,
            )
        except Exception:  # noqa: BLE001, S110 - telemetry is non-fatal
            pass
