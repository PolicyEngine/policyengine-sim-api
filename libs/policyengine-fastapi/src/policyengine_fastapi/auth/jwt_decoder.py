import logging

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Based on the auth0 article: https://auth0.com/blog/build-and-secure-fastapi-server-with-auth0/

# Use standard python logging
LOG = logging.getLogger(__name__)


class JWTDecoder:
    """
    Parse and verify the JWT, verifying the correct issuer and audience and returning
    the parsed token with all fields.
    """

    def __init__(self, issuer: str, audience: str, auto_error: bool = True) -> None:
        self.issuer = issuer
        self.audience = audience
        self.auto_error = auto_error
        jwks_url = f"{self.issuer}.well-known/jwks.json"
        self.jwks_client = jwt.PyJWKClient(jwks_url)

    def __call__(
        self,
        token: HTTPAuthorizationCredentials | None = Depends(
            HTTPBearer(auto_error=False)
        ),
    ) -> dict[str, str] | None:
        try:
            return self._validate(token)
        except HTTPException as err:
            if self.auto_error:
                LOG.info("Invalid auth token: Throwing 4XX error")
                raise err
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
            LOG.error(f"Unable to get public key for JWT validation: {error}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        except jwt.exceptions.DecodeError as error:
            LOG.error(f"Unable to get public key for JWT validation: {error}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        try:
            payload = jwt.decode(
                token.credentials,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
            )
        except Exception as error:
            LOG.info(f"Invalid bearer token: {error}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return payload
