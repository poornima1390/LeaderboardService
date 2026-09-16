"""Configuration, loaded exclusively from the environment.

Spec.md §8: config comes from environment variables, there are no hardcoded
values, and **the service fails to start** if a required variable is missing or
malformed. A service that boots with a default API key is worse than one that
refuses to boot, so secrets have no defaults.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
Environment = Literal["local", "ci", "staging", "production"]

# Values that look deliberate but are not. Matched as a *prefix* rather than an
# exact string, so padding a placeholder out to the length minimum
# ("changemeccccccccc") does not sneak it past the check. Prefix rather than
# substring matching keeps the false-positive rate at effectively zero: a
# generated key that happens to *contain* "test" is plausible, one that starts
# with it is not.
_PLACEHOLDER_PREFIXES = (
    "changeme",
    "change-me",
    "change_me",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api-key",
    "api_key",
    "test",
    "todo",
    "xxx",
    "placeholder",
    "example",
    "insecure",
)

_MIN_SECRET_LENGTH = 16


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    """Validated application settings.

    Field names map case-insensitively to environment variables, so
    ``api_key`` is populated from ``API_KEY``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"
    log_level: LogLevel = "INFO"

    # --- Required -----------------------------------------------------------
    database_url: str
    api_key: SecretStr
    admin_api_key: SecretStr

    # Populated from DATABASE_URL's `sslmode` parameter by the pre-validator
    # below, which has to strip it from the URL (see _normalise_database_url).
    # Not read directly from the environment.
    db_ssl_mode: str | None = None

    # --- Optional -----------------------------------------------------------
    # Redis is a *derived* index (Spec.md D2), so its absence is a degraded
    # state rather than a fatal one: reads fall back to Postgres.
    redis_url: str | None = None

    outbox_sweep_interval_s: float = Field(default=5.0, gt=0, le=300)

    # Set to 0 behind a transaction-mode pooler (PgBouncer / DO connection
    # pool): prepared statements do not survive a pooled connection being
    # handed to another client (Spec.md §8).
    db_statement_cache_size: int = Field(default=100, ge=0)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)

    # Request bodies above this are rejected with 413 (Spec.md §5).
    max_request_body_bytes: int = Field(default=8 * 1024, gt=0)

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @model_validator(mode="before")
    @classmethod
    def _extract_ssl_mode(cls, values: Any) -> Any:
        """Lift ``sslmode`` out of DATABASE_URL before the URL validator strips it.

        Runs first, so it sees the URL exactly as supplied regardless of whether
        it came from the process environment or a ``.env`` file.
        """
        if not isinstance(values, dict):
            return values
        raw = values.get("database_url") or values.get("DATABASE_URL") or ""
        if isinstance(raw, str) and raw:
            for key, value in parse_qsl(urlsplit(raw).query):
                if key == "sslmode" and value not in ("", "disable"):
                    values["db_ssl_mode"] = value
        return values

    @field_validator("api_key", "admin_api_key")
    @classmethod
    def _reject_weak_secrets(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < _MIN_SECRET_LENGTH:
            raise ValueError(f"must be at least {_MIN_SECRET_LENGTH} characters")

        normalised = raw.strip().lower()
        if normalised.startswith(_PLACEHOLDER_PREFIXES):
            raise ValueError("looks like a placeholder; generate one with `openssl rand -hex 32`")
        # "aaaaaaaaaaaaaaaaaaaa" clears the length check while carrying almost
        # no entropy. Eight distinct characters is a low bar that every real
        # generated key clears and every lazy one fails.
        if len(set(normalised)) < 8:
            raise ValueError(
                "has too few distinct characters to be a real key; "
                "generate one with `openssl rand -hex 32`"
            )
        return value

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Coerce a libpq-style URL into one SQLAlchemy's asyncpg dialect accepts.

        Two real deployment blockers are handled here, because DigitalOcean
        Managed Postgres hands out a URL that trips both:

        1. The scheme is ``postgresql://``, which SQLAlchemy resolves to the
           *synchronous* psycopg driver. It must be ``postgresql+asyncpg://``.
        2. The URL carries ``?sslmode=require``. The asyncpg dialect does not
           understand ``sslmode`` and raises on it — TLS is configured through
           ``connect_args={"ssl": ...}`` instead (see `ssl_mode` below), so the
           parameter is stripped here and re-read from the original value.
        """
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")

        parts = urlsplit(value)
        if parts.scheme in ("postgres", "postgresql"):
            parts = parts._replace(scheme="postgresql+asyncpg")
        elif parts.scheme != "postgresql+asyncpg":
            raise ValueError(
                f"unsupported scheme {parts.scheme!r}; expected postgresql:// "
                "or postgresql+asyncpg://"
            )

        if not parts.hostname:
            raise ValueError("must include a host")

        query = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "sslmode"
        ]
        parts = parts._replace(query=urlencode(query))
        return urlunsplit(parts)

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        scheme = urlsplit(value).scheme
        if scheme not in ("redis", "rediss"):
            raise ValueError(f"unsupported scheme {scheme!r}; expected redis:// or rediss://")
        return value

    @model_validator(mode="after")
    def _production_requires_tls(self) -> Settings:
        """Refuse to run in production against an unencrypted managed database.

        Cheap to check, and the failure mode it prevents — credentials in
        plaintext across a VPC — is not one worth discovering later.
        """
        if self.environment == "production":
            if self.db_ssl_mode is None:
                raise ValueError(
                    "DATABASE_URL must request TLS in production "
                    "(append ?sslmode=require to the URL)"
                )
            if self.redis_url is not None and urlsplit(self.redis_url).scheme != "rediss":
                raise ValueError("REDIS_URL must use rediss:// (TLS) in production")
        return self

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def redis_enabled(self) -> bool:
        return self.redis_url is not None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructing them on first use.

    Cached so the environment is read once. Any validation failure is
    re-raised as :class:`ConfigError` with every problem listed, so a
    misconfigured deploy fails loudly at startup with an actionable message
    rather than at the first request.
    """
    try:
        # Field values come from the environment; the pydantic mypy plugin
        # understands this, so no call-arg suppression is needed.
        return Settings()
    except Exception as exc:
        raise ConfigError(
            "Invalid configuration. The service will not start until this is fixed.\n"
            f"{exc}\n"
            "See .env.example for the full list of variables."
        ) from exc
