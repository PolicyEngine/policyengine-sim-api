"""Environment-driven settings for the executor app (read from ``.env``)."""

from enum import Enum
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(Enum):
    """Deployment environment the app runs in."""

    DESKTOP = "desktop"
    PRODUCTION = "production"


class AppSettings(BaseSettings):
    """Executor settings loaded from environment variables and a local ``.env`` file."""

    environment: Environment = Environment.DESKTOP

    @field_validator("environment", mode="before")
    @classmethod
    def strip_environment(cls, v: object) -> object:
        """Strip whitespace from a string ``environment`` before enum parsing."""
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

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings() -> AppSettings:
    """Return the process-wide settings, loaded once and cached."""
    return AppSettings()
