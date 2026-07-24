"""Runtime configuration for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


PRODUCTION_ENVIRONMENTS = frozenset({"main", "prod", "production"})


class ConfigurationError(RuntimeError):
    """Raised when the service cannot start safely."""


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _normalized_issuer(value: str) -> str:
    return f"{value.rstrip('/')}/" if value else ""


@dataclass(frozen=True)
class Settings:
    """All configuration required by the Stage 5 control-plane proxy."""

    environment: str
    public_url: str
    auth_required: bool
    auth_issuer: str
    auth_audience: str
    old_gateway_url: str
    old_gateway_auth_issuer: str
    old_gateway_auth_audience: str
    old_gateway_auth_client_id: str
    old_gateway_auth_client_secret: str
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 25.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("APP_ENVIRONMENT", "local").lower(),
            public_url=os.getenv("SIMULATION_ENTRYPOINT_PUBLIC_URL", ""),
            auth_required=_truthy(
                os.getenv("SIMULATION_ENTRYPOINT_AUTH_REQUIRED"),
                default=True,
            ),
            auth_issuer=_normalized_issuer(
                os.getenv("SIMULATION_ENTRYPOINT_AUTH_ISSUER", "")
            ),
            auth_audience=os.getenv("SIMULATION_ENTRYPOINT_AUTH_AUDIENCE", ""),
            old_gateway_url=os.getenv("OLD_GATEWAY_URL", "").rstrip("/"),
            old_gateway_auth_issuer=_normalized_issuer(
                os.getenv("OLD_GATEWAY_AUTH_ISSUER", "")
            ),
            old_gateway_auth_audience=os.getenv("OLD_GATEWAY_AUTH_AUDIENCE", ""),
            old_gateway_auth_client_id=os.getenv("OLD_GATEWAY_AUTH_CLIENT_ID", ""),
            old_gateway_auth_client_secret=os.getenv(
                "OLD_GATEWAY_AUTH_CLIENT_SECRET", ""
            ),
            connect_timeout_seconds=float(
                os.getenv("OLD_GATEWAY_CONNECT_TIMEOUT_SECONDS", "5")
            ),
            request_timeout_seconds=float(
                os.getenv("OLD_GATEWAY_REQUEST_TIMEOUT_SECONDS", "25")
            ),
        )

    @property
    def production(self) -> bool:
        return self.environment in PRODUCTION_ENVIRONMENTS

    def validate(self) -> None:
        if self.production and not self.auth_required:
            raise ConfigurationError(
                "Caller authentication cannot be disabled in production."
            )

        if bool(self.auth_issuer) != bool(self.auth_audience):
            raise ConfigurationError(
                "Set both SIMULATION_ENTRYPOINT_AUTH_ISSUER and SIMULATION_ENTRYPOINT_AUTH_AUDIENCE."
            )
        if self.auth_required and not self.auth_issuer:
            raise ConfigurationError(
                "Caller authentication is required but issuer/audience are missing."
            )

        required_backend = {
            "OLD_GATEWAY_URL": self.old_gateway_url,
            "OLD_GATEWAY_AUTH_ISSUER": self.old_gateway_auth_issuer,
            "OLD_GATEWAY_AUTH_AUDIENCE": self.old_gateway_auth_audience,
            "OLD_GATEWAY_AUTH_CLIENT_ID": self.old_gateway_auth_client_id,
            "OLD_GATEWAY_AUTH_CLIENT_SECRET": self.old_gateway_auth_client_secret,
        }
        missing = [name for name, value in required_backend.items() if not value]
        if missing:
            raise ConfigurationError(
                f"Missing old-gateway configuration: {', '.join(missing)}."
            )

        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ConfigurationError("Old-gateway timeouts must be positive.")

        public_host = urlparse(self.public_url).hostname
        upstream_host = urlparse(self.old_gateway_url).hostname
        if public_host and upstream_host and public_host == upstream_host:
            raise ConfigurationError(
                "OLD_GATEWAY_URL must not point to the Simulation Entrypoint itself."
            )
