"""Service settings, read from DRAVENPDF_* environment variables."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dravenpdf.options import MAX_TIMEOUT_MS


class SigningKeyConfig(BaseModel):
    """One entry of DRAVENPDF_SIGNING_KEYS."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    pkcs12: Path
    """Path to the .p12 / .pfx file on the server."""
    password_file: Path | None = None
    """File holding the passphrase (preferred: e.g. a mounted secret)."""
    password: SecretStr | None = None

    @model_validator(mode="after")
    def _one_password_source(self) -> SigningKeyConfig:
        if self.password is not None and self.password_file is not None:
            raise ValueError("give password or password_file, not both")
        return self

    def read_password(self) -> SecretStr | None:
        if self.password_file is not None:
            return SecretStr(self.password_file.read_text().rstrip("\r\n"))
        return self.password


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRAVENPDF_", extra="ignore", hide_input_in_errors=True
    )

    api_key: SecretStr | None = None
    auth_disabled: bool = False
    max_concurrency: int = Field(default=4, ge=1)
    max_queue: int = Field(default=16, ge=0)
    render_timeout_ms: int = Field(default=30_000, gt=0, le=MAX_TIMEOUT_MS)
    max_body_mb: float = Field(default=25, gt=0)
    # Output limits: a small upload can ask for a lot of work.
    max_output_mb: float = Field(default=100, gt=0)
    max_image_megapixels: float = Field(default=40, gt=0)
    # Comma-separated; empty/unset means "any public host".
    allowed_hosts: str = ""
    browser_recycle_after: int = Field(default=500, ge=1)
    log_level: str = "INFO"
    # Signing: {"name": {"pkcs12": "/keys/x.p12", "password_file": "/run/secrets/x"}}.
    # Keys stay on the server; requests refer to them by name.
    signing_keys: dict[str, SigningKeyConfig] = Field(default_factory=dict)
    timestamp_url: str | None = None
    """RFC 3161 timestamp server used when a sign request asks for a timestamp."""
    trust_roots: str = ""
    """Comma-separated PEM/DER files of certificates that /v1/pdf/verify trusts."""

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

    @property
    def max_output_bytes(self) -> int:
        return int(self.max_output_mb * 1024 * 1024)

    @property
    def max_image_pixels(self) -> int:
        return int(self.max_image_megapixels * 1_000_000)

    @property
    def trust_root_files(self) -> list[Path]:
        return [Path(p.strip()) for p in self.trust_roots.split(",") if p.strip()]
