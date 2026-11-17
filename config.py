"""
Application configuration

This module exposes a singleton `settings` instance that can be imported
throughout the project (e.g., from config import settings).
"""
import re
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# scheme://host[:port], nothing after: what a browser sends in its Origin header
_ORIGIN = re.compile(r"https?://[a-z0-9.-]+(:\d{1,5})?")


def parse_origins(value: str) -> list[str]:
    """
    Comma-separated origins -> list, normalized the way browsers send them: spaces trimmed, a trailing "/"
    dropped, lowercased. A path, a missing scheme or an empty list is an error: the browser would otherwise
    block the website silently.

    Raises:
        ValueError: An entry isn't an origin, or there is none
    """
    origins = []
    for entry in value.split(","):
        origin = entry.strip().rstrip("/").lower()
        if not origin:
            continue
        if not _ORIGIN.fullmatch(origin):
            raise ValueError(f"{entry.strip()!r} is not an origin (scheme://host[:port], no path), e.g. https://fcpxstudio.shop")
        if origin not in origins:
            origins.append(origin)
    if not origins:
        raise ValueError("needs at least one origin, e.g. https://fcpxstudio.shop")
    return origins


class EventAPISettings(BaseSettings):
    # Load .env
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # never print secrets in validation errors
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
    # Origins allowed to call the web routes from a browser (CORS), comma-separated, e.g.
    # "https://fcpxstudio.shop,https://www.fcpxstudio.shop". Normalized by parse_origins. Required
    WEBSITE_ORIGIN: str
    # How long a claim waits for the order's licenses before answering `pending`
    CLAIM_WAIT_SECONDS: float = 20
    # Abuse limits (app/website/limits.py)
    CLAIM_MAX_WAITS_PER_IP: int = 3
    CLAIM_MAX_REQUESTS_PER_MINUTE: int = 10
    CLAIM_MAX_WAITS: int = 500



    @field_validator("WEBSITE_ORIGIN")
    @classmethod
    def valid_origins(cls, value: str) -> str:
        return ",".join(parse_origins(value))

    @property
    def website_origins(self) -> list[str]:
        """WEBSITE_ORIGIN as a list (already normalized)"""
        return self.WEBSITE_ORIGIN.split(",")


settings = EventAPISettings() # type: ignore