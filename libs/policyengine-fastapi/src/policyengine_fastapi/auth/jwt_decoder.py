"""FastAPI dependency that verifies RS256 bearer JWTs against an issuer's JWKS."""

import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Based on the auth0 article: https://auth0.com/blog/build-and-secure-fastapi-server-with-auth0/

# Use standard python logging
LOG = logging.getLogger(__name__)


class JWTDecoder:
    """Callable FastAPI dependency that verifies a bearer JWT and returns its claims.

    Tokens must be RS256-signed by a key from the issuer's JWKS and carry the
    configured issuer and audience. A missing or invalid token raises HTTP 403,
    or yields None when ``auto_error`` is False.
    """

    def __init__(self, issuer: str, audience: str, *, auto_error: bool = True) -> None:
        """Store the expected claims and create a JWKS client for the issuer.

        Keys are fetched from ``{issuer}.well-known/jwks.json``, so ``issuer``
        must end with a slash.
        """
        self.issuer = issuer
        self.audience = audience
        self.auto_error = auto_error
        jwks_url = f"{self.issuer}.well-known/jwks.json"
        self.jwks_client = jwt.PyJWKClient(jwks_url)

    def __call__(
        self,
        token: Annotated[
            HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
        ],
    ) -> dict[str, str] | None:
        """Return the verified token's claims.

        A missing or invalid token raises HTTP 403, or returns None when
        ``auto_error`` is False.
        """
        try:
            return self._validate(token)
        except HTTPException:
            if self.auto_error:
                LOG.info("Invalid auth token: Throwing 4XX error")
                raise
            LOG.info("Ignoring invalid auth token")
        return None

    def _validate(self, token: HTTPAuthorizationCredentials | None) -> dict[str, str]:
        if token is None:
            LOG.info("No bearer token on request")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        # This gets the 'kid' from the passed token
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(
                token.credentials
            ).key
        except jwt.exceptions.PyJWKClientError as error:
            LOG.error("Unable to get public key for JWT validation: %s", error)  # noqa: TRY400  # callers control the token, so a traceback per bad token would let them flood the logs
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error
        except jwt.exceptions.DecodeError as error:
            LOG.error("Unable to get public key for JWT validation: %s", error)  # noqa: TRY400  # callers control the token, so a traceback per bad token would let them flood the logs
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error

        try:
            payload = jwt.decode(
                token.credentials,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        # Any decode or claim-validation failure must become a 403, never a 500.
        except Exception as error:
            LOG.info("Invalid bearer token: %s", error)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from error
        return payload
