from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(Enum):
    DESKTOP = "desktop"
    PRODUCTION = "production"


class AppSettings(BaseSettings):
    environment: Environment = Environment.DESKTOP

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache
def get_settings():
    return AppSettings()
