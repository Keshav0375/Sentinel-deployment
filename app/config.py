"""App configuration — env vars (or a local `.env`) via pydantic-settings.

On App Service every field comes from an app setting; locally the defaults serve.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "local-dev"
    dd_service: str = "sentinel-watchtower"
    dd_env: str = "dev"
    port: int = 8000
