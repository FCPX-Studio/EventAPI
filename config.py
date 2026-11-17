"""
Application configuration

This module exposes a singleton `settings` instance that can be imported
throughout the project (e.g., from config import settings).
"""
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EventAPISettings(BaseSettings):
    # Load .env
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # FastAPI application
    APP_NAME: str = "EventAPI"
    APP_DESCRIPTION: str = "Expose event sending and subscription endpoints to external services"
    APP_VERSION: str = "1.0"
    # API url infos: api.fcpxstudio.shop/<API_NAME>/<API_VERSION>/
    API_NAME: str = "events"
    API_VERSION: str = "v1" 

    # Redis
    REDIS_HOST: SecretStr
    REDIS_PORT: SecretStr
    REDIS_PASSWORD: SecretStr

    # Paddle
    PADDLE_ENVIRONMENT: Literal["sandbox", "production"] = "sandbox"
    PADDLE_WEBHOOK_SECRET: SecretStr

    # Website (license claims from the confirmation page, app/routers/web.py)
    # Origin allowed to call the web routes from a browser (CORS), e.g. "https://fcpxstudio.shop". Required
    WEBSITE_ORIGIN: str
    # How long a claim waits for the order's licenses before answering `pending`
    CLAIM_WAIT_SECONDS: float = 20
    # Abuse limits (app/website/limits.py)
    CLAIM_MAX_WAITS_PER_IP: int = 3
    CLAIM_MAX_REQUESTS_PER_MINUTE: int = 10
    CLAIM_MAX_WAITS: int = 500



settings = EventAPISettings() # type: ignore