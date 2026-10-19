"""Runtime configuration for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from policyengine_simulation_contract.stage12_manifest import (
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    assert_separate_manifest_names,
)

PRODUCTION_ENVIRONMENTS = frozenset({"main", "prod", "production"})
MODAL_GATEWAY_HOST_SUFFIX = ".modal.run"
CLOUD_TASKS_QUEUE_PATTERN = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/queues/[A-Za-z0-9-]{1,100}"
)
SERVICE_ACCOUNT_PATTERN = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com"
)


class ConfigurationError(RuntimeError):
    """Raised when the service cannot start safely."""


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _binary_flag(name: str) -> bool:
    value = os.getenv(name)
    if value is None or value == "0":
        return False
    if value == "1":
        return True
    msg = f"{name} must be exactly 0 or 1."
    raise ConfigurationError(msg)


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
        msg = f"{name} must be an absolute HTTPS URL."
        raise ConfigurationError(msg)
    if parsed.params or parsed.query or parsed.fragment:
        msg = f"{name} must not contain parameters, a query string, or a fragment."
        raise ConfigurationError(msg)
    if not allow_path and parsed.path not in {"", "/"}:
        msg = f"{name} must not contain a path."
        raise ConfigurationError(msg)


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
    # False records callers that lack a route permission and lets them
    # through; True rejects them with 403.
    enforce_permissions: bool = False
    connect_timeout_seconds: float = 5.0
    request_timeout_seconds: float = 25.0
    stage12_enabled: bool = False
    stage12_v2_manifest_name: str = V2_VERSION_MANIFEST_NAME
    stage12_v2_manifest_environment: str = ""
    stage12_v2_worker_version: str | None = None
    stage12_database_url: str = ""
    stage12_artifact_bucket: str = ""
    stage12_retention_days: int = 30
    # The runtime role's URL for the Simulation Entrypoint's own schema. Empty
    # outside deployed environments; nothing on the old-gateway or Stage 12
    # paths reads it.
    simulation_database_url: str = ""
    # Background collection of simulation job results. Cloud Tasks calls the
    # collection route of RECONCILIATION_TARGET_URL with a Google OIDC token of
    # RECONCILIATION_INVOKER_SERVICE_ACCOUNT; Cloud Scheduler calls the sweep
    # route with the same identity.
    reconciliation_queue: str = ""
    reconciliation_invoker_service_account: str = ""
    reconciliation_target_url: str = ""
    simulation_modal_environment: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        """Read settings from environment variables, applying defaults.

        Only STAGE12_ENABLED is checked here (it must be 0 or 1); call validate
        for everything else. Auth is required unless explicitly disabled.
        """
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
            enforce_permissions=_binary_flag(
                "SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS"
            ),
            connect_timeout_seconds=float(
                os.getenv("OLD_GATEWAY_CONNECT_TIMEOUT_SECONDS", "5")
            ),
            request_timeout_seconds=float(
                os.getenv("OLD_GATEWAY_REQUEST_TIMEOUT_SECONDS", "25")
            ),
            stage12_enabled=_binary_flag("STAGE12_ENABLED"),
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
            simulation_database_url=os.getenv("SIMULATION_DATABASE_URL", ""),
            reconciliation_queue=os.getenv("RECONCILIATION_QUEUE", ""),
            reconciliation_invoker_service_account=os.getenv(
                "RECONCILIATION_INVOKER_SERVICE_ACCOUNT", ""
            ),
            reconciliation_target_url=os.getenv("RECONCILIATION_TARGET_URL", "").rstrip(
                "/"
            ),
            simulation_modal_environment=os.getenv("SIMULATION_MODAL_ENVIRONMENT", ""),
        )

    @property
    def production(self) -> bool:
        """Whether this is a production environment, where auth must stay on."""
        return self.environment in PRODUCTION_ENVIRONMENTS

    @property
    def stage12_resources_configured(self) -> bool:
        """Whether the Stage 12 manifest environment, database and bucket are set."""
        return all(
            (
                self.stage12_v2_manifest_environment,
                self.stage12_database_url,
                self.stage12_artifact_bucket,
            )
        )

    @property
    def _simulation_job_settings(self) -> dict[str, str]:
        return {
            "SIMULATION_DATABASE_URL": self.simulation_database_url,
            "RECONCILIATION_QUEUE": self.reconciliation_queue,
            "RECONCILIATION_INVOKER_SERVICE_ACCOUNT": (
                self.reconciliation_invoker_service_account
            ),
            "RECONCILIATION_TARGET_URL": self.reconciliation_target_url,
            "SIMULATION_MODAL_ENVIRONMENT": self.simulation_modal_environment,
        }

    @property
    def simulation_jobs_configured(self) -> bool:
        """Whether the job store, its queue and its Modal environment are all set."""
        return all(self._simulation_job_settings.values())

    def _validate_simulation_jobs(self) -> None:
        reconciliation = {
            name: value
            for name, value in self._simulation_job_settings.items()
            if name != "SIMULATION_DATABASE_URL"
        }
        if not any(reconciliation.values()):
            return
        missing = [
            name for name, value in self._simulation_job_settings.items() if not value
        ]
        if missing:
            msg = f"Simulation jobs require: {', '.join(missing)}."
            raise ConfigurationError(msg)
        if not CLOUD_TASKS_QUEUE_PATTERN.fullmatch(self.reconciliation_queue):
            msg = (
                "RECONCILIATION_QUEUE must be a full Cloud Tasks queue name: "
                "projects/<project>/locations/<location>/queues/<queue>."
            )
            raise ConfigurationError(msg)
        if not SERVICE_ACCOUNT_PATTERN.fullmatch(
            self.reconciliation_invoker_service_account
        ):
            msg = (
                "RECONCILIATION_INVOKER_SERVICE_ACCOUNT must be a Google service "
                "account email."
            )
            raise ConfigurationError(msg)
        _validate_https_url(
            "RECONCILIATION_TARGET_URL",
            self.reconciliation_target_url,
            allow_path=False,
        )

    def validate(self) -> None:
        """Raise ConfigurationError unless the service can start safely.

        Called from the app lifespan, so a misconfigured revision fails at
        startup rather than on its first request. Stage 12 resources must be
        configured all together or not at all.
        """
        if self.production and not self.auth_required:
            msg = "Caller authentication cannot be disabled in production."
            raise ConfigurationError(msg)

        if bool(self.auth_issuer) != bool(self.auth_audience):
            msg = (
                "Set both SIMULATION_ENTRYPOINT_AUTH_ISSUER and "
                "SIMULATION_ENTRYPOINT_AUTH_AUDIENCE."
            )
            raise ConfigurationError(msg)
        if self.auth_required and not self.auth_issuer:
            msg = "Caller authentication is required but issuer/audience are missing."
            raise ConfigurationError(msg)
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
            msg = f"Missing old-gateway configuration: {', '.join(missing)}."
            raise ConfigurationError(msg)

        _validate_https_url(
            "OLD_GATEWAY_AUTH_ISSUER",
            self.old_gateway_auth_issuer,
        )

        if self.connect_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            msg = "Old-gateway timeouts must be positive."
            raise ConfigurationError(msg)

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
            msg = "OLD_GATEWAY_URL must point to the existing Modal gateway."
            raise ConfigurationError(msg)

        try:
            assert_separate_manifest_names(
                v1_name=V1_ROUTING_STATE_NAME,
                v2_name=self.stage12_v2_manifest_name,
            )
        except ValueError as error:
            raise ConfigurationError(str(error)) from error
        if self.stage12_retention_days != 30:
            msg = "STAGE12_RETENTION_DAYS must be exactly 30."
            raise ConfigurationError(msg)
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
            msg = (
                "STAGE12_ENABLED=1 requires the Stage 12 manifest, database, "
                "and artifact resources."
            )
            raise ConfigurationError(msg)
        if self.stage12_database_url and not self.stage12_database_url.startswith(
            "postgresql://"
        ):
            msg = "STAGE12_DATABASE_URL must use the canonical postgresql:// scheme."
            raise ConfigurationError(msg)
        if self.simulation_database_url and not self.simulation_database_url.startswith(
            "postgresql://"
        ):
            msg = "SIMULATION_DATABASE_URL must use the canonical postgresql:// scheme."
            raise ConfigurationError(msg)
        self._validate_simulation_jobs()
