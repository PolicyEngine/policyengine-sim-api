"""Verify the Google OIDC tokens Cloud Tasks and Cloud Scheduler send.

The internal simulation job routes are reachable on the public service URL,
so each request must carry a Google-signed ID token whose audience is the
service URL and whose email is the configured invoker service account.
"""

from __future__ import annotations

from typing import Protocol

import jwt

GOOGLE_CERTIFICATES_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "email"]


class InternalCallerRejectedError(Exception):
    """The token is missing, invalid, or from another identity."""


class SigningKeySource(Protocol):
    """Where verification finds the key that signed a token."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
        """Return the key named by the token's header."""
        ...


class GoogleOidcVerifier:
    """Accept only Google ID tokens of one service account for one audience."""

    def __init__(
        self,
        *,
        audience: str,
        service_account_email: str,
        jwks_client: SigningKeySource | None = None,
    ) -> None:
        """Bind the expected audience and account; keys come from Google."""
        self.audience = audience
        self.service_account_email = service_account_email
        self.jwks_client = jwks_client or jwt.PyJWKClient(GOOGLE_CERTIFICATES_URL)

    def verify(self, token: str) -> None:
        """Return when ``token`` is valid; otherwise raise the rejection.

        The rejection's message names only the failure type, never the token.
        Fetching Google's keys may block, so callers run this off the event loop.
        """
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=GOOGLE_ISSUERS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as error:
            raise InternalCallerRejectedError(type(error).__name__) from error
        if (
            claims.get("email") != self.service_account_email
            or claims.get("email_verified") is not True
        ):
            msg = "unexpected_service_account"
            raise InternalCallerRejectedError(msg)
