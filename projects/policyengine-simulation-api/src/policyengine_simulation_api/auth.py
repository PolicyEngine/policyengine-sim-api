"""Inbound bearer authentication for protected Simulation API routes."""

from __future__ import annotations

from functools import lru_cache
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from policyengine_observability import record_event

from policyengine_simulation_api.config import Settings


_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class JWTDecoder:
    """Validate Auth0 RS256 access tokens without database-layer dependencies."""

    def __init__(self, issuer: str, audience: str):
        self.issuer = issuer
        self.audience = audience
        self.jwks_client = jwt.PyJWKClient(f"{issuer}.well-known/jwks.json")

    def __call__(
        self,
        token: HTTPAuthorizationCredentials | None,
    ) -> dict[str, str]:
        if token is None:
            record_event("simulation_api_auth_rejected", reason="missing_token")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(
                token.credentials
            ).key
            return jwt.decode(
                token.credentials,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        except Exception as error:
            reason = type(error).__name__
            logger.info(
                "invalid_simulation_api_bearer_token",
                extra={"error_type": reason},
            )
            record_event("simulation_api_auth_rejected", reason=reason)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error


@lru_cache(maxsize=8)
def _decoder(issuer: str, audience: str) -> JWTDecoder:
    return JWTDecoder(issuer=issuer, audience=audience)


class CallerAuthenticator:
    """FastAPI dependency that preserves the gateway's JWT contract."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(
        self,
        token: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> dict[str, str] | None:
        if not self.settings.auth_required:
            return None
        return _decoder(
            self.settings.auth_issuer,
            self.settings.auth_audience,
        )(token)


def reset_decoder_cache() -> None:
    """Clear the decoder cache for tests and credential rotations."""

    _decoder.cache_clear()
