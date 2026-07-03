from enum import Enum
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from policyengine_fastapi.observability import (
    ObservabilityConfig,
    TracerCaptureMode,
    parse_header_value_pairs,
)


class Environment(Enum):
    DESKTOP = "desktop"
    PRODUCTION = "production"


class AppSettings(BaseSettings):
    environment: Environment = Environment.DESKTOP

    @field_validator("environment", mode="before")
    @classmethod
    def strip_environment(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    ot_service_name: str = "YOUR_OT_SERVICE_NAME"
    """
    service name used by opentelemetry when reporting trace information
    """
    ot_service_instance_id: str = "YOUR_OT_INSTANCE_ID"
    """
    instance id used by opentelemetry when reporting trace information
    """

    observability_enabled: bool = False
    observability_shadow_mode: bool = True
    observability_service_name: str = "policyengine-simulation-executor"
    observability_environment: str | None = None
    observability_otlp_endpoint: str | None = None
    observability_otlp_headers: str = ""
    observability_artifact_bucket: str | None = None
    observability_artifact_prefix: str = "simulation-observability"
    observability_tracer_capture_mode: TracerCaptureMode = TracerCaptureMode.DISABLED
    observability_slow_run_threshold_seconds: float = 30.0

    model_config = SettingsConfigDict(env_file=".env")

    @field_validator("observability_otlp_headers", mode="before")
    @classmethod
    def strip_observability_otlp_headers(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @property
    def observability(self) -> ObservabilityConfig:
        environment = self.observability_environment or self.environment.value
        return ObservabilityConfig(
            enabled=self.observability_enabled,
            shadow_mode=self.observability_shadow_mode,
            service_name=self.observability_service_name,
            environment=environment,
            otlp_endpoint=self.observability_otlp_endpoint,
            otlp_headers=parse_header_value_pairs(self.observability_otlp_headers),
            artifact_bucket=self.observability_artifact_bucket,
            artifact_prefix=self.observability_artifact_prefix,
            tracer_capture_mode=self.observability_tracer_capture_mode,
            slow_run_threshold_seconds=self.observability_slow_run_threshold_seconds,
        )


@lru_cache
def get_settings():
    return AppSettings()
