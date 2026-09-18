"""Runtime configuration for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from policyengine_simulation_contract.stage12_manifest import (
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    assert_separate_manifest_names,
)

PRODUCTION_ENVIRONMENTS = frozenset({"main", "prod", "production"})
MODAL_GATEWAY_HOST_SUFFIX = ".modal.run"


class ConfigurationError(RuntimeError):
    """Raised when the service cannot start safely."""


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _stage12_enabled(value: str | None) -> bool:
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    raise ConfigurationError("STAGE12_ENABLED must be exactly 0 or 1.")


def _normalized_issuer(value: str) -> str:
    return f"{value.rstrip('/')}/" if value else ""


def _validate_https_url(
    name: str,
    value: str,
    *,
    allow_path: bool = True,
) -> None:
    parsed = urlparse(value)
    if (
        value != value.strip()
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError(f"{name} must be an absolute HTTPS URL.")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigurationError(
            f"{name} must not contain parameters, a query string, or a fragment."
        )
    if not allow_path and parsed.path not in {"", "/"}:
        raise ConfigurationError(f"{name} must not contain a path.")


@dataclass(frozen=True)
class Settings:
    """All configuration required by the Stage 5 control-plane proxy."""

    environment: str
    revision: str
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
    stage12_enabled: bool = False
    stage12_v2_manifest_name: str = V2_VERSION_MANIFEST_NAME
    stage12_v2_manifest_environment: str = ""
    stage12_v2_worker_version: str | None = None
    stage12_database_url: str = ""
    stage12_artifact_bucket: str = ""
    stage12_retention_days: int = 30

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("APP_ENVIRONMENT", "local").lower(),
            revision=os.getenv("K_REVISION", ""),
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
            stage12_enabled=_stage12_enabled(os.getenv("STAGE12_ENABLED")),
            stage12_v2_manifest_name=os.getenv(
                "STAGE12_V2_MANIFEST_NAME",
                V2_VERSION_MANIFEST_NAME,
            ),
            stage12_v2_manifest_environment=os.getenv(
                "STAGE12_V2_MANIFEST_ENVIRONMENT",
                "",
            ),
            stage12_v2_worker_version=(os.getenv("STAGE12_V2_WORKER_VERSION") or None),
            stage12_database_url=os.getenv("STAGE12_DATABASE_URL", ""),
            stage12_artifact_bucket=os.getenv("STAGE12_ARTIFACT_BUCKET", ""),
            stage12_retention_days=int(os.getenv("STAGE12_RETENTION_DAYS", "30")),
        )

    @property
    def production(self) -> bool:
        return self.environment in PRODUCTION_ENVIRONMENTS

    @property
    def stage12_resources_configured(self) -> bool:
        return all(
            (
                self.stage12_v2_manifest_environment,
                self.stage12_database_url,
                self.stage12_artifact_bucket,
            )
        )

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
        if self.auth_issuer:
            _validate_https_url(
                "SIMULATION_ENTRYPOINT_AUTH_ISSUER",
                self.auth_issuer,
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

        _validate_https_url(
            "OLD_GATEWAY_AUTH_ISSUER",
            self.old_gateway_auth_issuer,
        )

        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ConfigurationError("Old-gateway timeouts must be positive.")

        _validate_https_url(
            "OLD_GATEWAY_URL",
            self.old_gateway_url,
            allow_path=False,
        )
        upstream = urlparse(self.old_gateway_url)
        upstream_hostname = upstream.hostname
        if upstream_hostname is None or not upstream_hostname.endswith(
            MODAL_GATEWAY_HOST_SUFFIX
        ):
            raise ConfigurationError(
                "OLD_GATEWAY_URL must point to the existing Modal gateway."
            )

        try:
            assert_separate_manifest_names(
                v1_name=V1_ROUTING_STATE_NAME,
                v2_name=self.stage12_v2_manifest_name,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        if self.stage12_retention_days != 30:
            raise ConfigurationError("STAGE12_RETENTION_DAYS must be exactly 30.")
        required_stage12 = {
            "STAGE12_V2_MANIFEST_ENVIRONMENT": self.stage12_v2_manifest_environment,
            "STAGE12_DATABASE_URL": self.stage12_database_url,
            "STAGE12_ARTIFACT_BUCKET": self.stage12_artifact_bucket,
        }
        configured_count = sum(bool(value) for value in required_stage12.values())
        if configured_count not in {0, len(required_stage12)}:
            missing_stage12 = [
                name for name, value in required_stage12.items() if not value
            ]
            raise ConfigurationError(
                "Stage 12 resources require: " + ", ".join(missing_stage12) + "."
            )
        if self.stage12_enabled and not self.stage12_resources_configured:
            raise ConfigurationError(
                "STAGE12_ENABLED=1 requires the Stage 12 manifest, database, "
                "and artifact resources."
            )
