"""Service settings, read from DRAVENPDF_* environment variables."""

from __future__ import annotations

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dravenpdf.options import MAX_TIMEOUT_MS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DRAVENPDF_", extra="ignore")

    api_key: SecretStr | None = None
    auth_disabled: bool = False
    max_concurrency: int = Field(default=4, ge=1)
    max_queue: int = Field(default=16, ge=0)
    render_timeout_ms: int = Field(default=30_000, gt=0, le=MAX_TIMEOUT_MS)
    max_body_mb: float = Field(default=25, gt=0)
    # Comma-separated; empty/unset means "any public host".
    allowed_hosts: str = ""
    browser_recycle_after: int = Field(default=500, ge=1)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _require_key(self) -> Settings:
        key = self.api_key.get_secret_value() if self.api_key else ""
        if not key and not self.auth_disabled:
            raise ValueError(
                "DRAVENPDF_API_KEY is not set. Set it, or set DRAVENPDF_AUTH_DISABLED=true "
                "for local development only."
            )
        return self

    @property
    def allowed_host_list(self) -> list[str] | None:
        hosts = [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        return hosts or None

    @property
    def max_body_bytes(self) -> int:
        return int(self.max_body_mb * 1024 * 1024)
