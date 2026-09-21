"""Google identity-token provider for the private persistence API."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token, service_account

STAGE12_PERSISTENCE_AUDIENCE = "https://policyengine.org/internal/stage12-persistence"


def _expiry_is_near(expiry: datetime | None) -> bool:
    if expiry is None:
        return True
    normalized = expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=UTC)
    return normalized <= datetime.now(UTC) + timedelta(minutes=1)


def _service_account_info(environment: dict[str, str]) -> dict[str, Any] | None:
    raw = environment.get("GOOGLE_APPLICATION_CREDENTIALS_JSON") or environment.get(
        "GCP_CREDENTIALS_JSON"
    )
    if not raw:
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        # Modal can preserve escaped newlines from a secret value. Decode one
        # JSON string layer without altering the credential fields themselves.
        parsed = json.loads(f'"{raw}"')
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    if not isinstance(parsed, dict):
        raise TypeError("Google service-account credentials must be a JSON object")
    return parsed


class GoogleIdentityTokenProvider:
    """Mint and cache a service-account identity token for one audience."""

    def __init__(
        self,
        audience: str = STAGE12_PERSISTENCE_AUDIENCE,
        *,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._audience = audience
        self._environment = environment if environment is not None else dict(os.environ)
        self._credentials: Credentials | None = None
        self._lock = Lock()

    def _load_credentials(self, request: GoogleAuthRequest) -> Credentials:
        service_account_info = _service_account_info(self._environment)
        if service_account_info is not None:
            return service_account.IDTokenCredentials.from_service_account_info(
                service_account_info,
                target_audience=self._audience,
            )
        return id_token.fetch_id_token_credentials(self._audience, request=request)

    def __call__(self) -> str:
        with self._lock:
            request = GoogleAuthRequest()
            if self._credentials is None:
                self._credentials = self._load_credentials(request)
            if not self._credentials.token or _expiry_is_near(self._credentials.expiry):
                self._credentials.refresh(request)
            token = self._credentials.token
            if not isinstance(token, str) or not token:
                raise RuntimeError("Google identity-token minting returned no token")
            return token
