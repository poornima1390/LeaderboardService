"""Configuration validation — the fail-fast guarantees of Spec.md §8."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import ConfigError, Settings, get_settings

BASE = {
    "environment": "local",
    "database_url": "postgresql+asyncpg://u:p@db:5432/leaderboard",
    "api_key": "a-sufficiently-long-key-value",
    "admin_api_key": "another-sufficiently-long-key",
}


def make(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


class TestDatabaseUrlNormalisation:
    """DigitalOcean hands out a URL that trips the asyncpg dialect twice."""

    def test_rewrites_bare_postgresql_scheme_to_asyncpg(self) -> None:
        settings = make(database_url="postgresql://u:p@db:5432/leaderboard")
        assert settings.database_url.startswith("postgresql+asyncpg://")

    def test_rewrites_postgres_alias_scheme(self) -> None:
        settings = make(database_url="postgres://u:p@db:5432/leaderboard")
        assert settings.database_url.startswith("postgresql+asyncpg://")

    def test_strips_sslmode_and_captures_it(self) -> None:
        """sslmode must leave the URL (the dialect rejects it) but not be lost."""
        settings = make(
            database_url="postgresql://u:p@do-db.ondigitalocean.com:25060/db?sslmode=require"
        )
        assert "sslmode" not in settings.database_url
        assert settings.db_ssl_mode == "require"

    def test_preserves_other_query_parameters(self) -> None:
        settings = make(
            database_url="postgresql://u:p@db:5432/leaderboard?sslmode=require&application_name=lb"
        )
        assert "application_name=lb" in settings.database_url
        assert "sslmode" not in settings.database_url

    def test_sslmode_disable_is_not_treated_as_tls(self) -> None:
        settings = make(database_url="postgresql://u:p@db:5432/db?sslmode=disable")
        assert settings.db_ssl_mode is None

    @pytest.mark.parametrize(
        "url",
        [
            "mysql://u:p@db:3306/leaderboard",
            "postgresql+psycopg://u:p@db:5432/leaderboard",
            "",
            "postgresql://",
        ],
    )
    def test_rejects_unusable_urls(self, url: str) -> None:
        with pytest.raises(ValidationError):
            make(database_url=url)


class TestSecretStrength:
    def test_rejects_short_key(self) -> None:
        with pytest.raises(ValidationError, match="at least 16"):
            make(api_key="short")

    @pytest.mark.parametrize("weak", ["changeme", "CHANGEME", "password", "secret"])
    def test_rejects_placeholder_key(self, weak: str) -> None:
        """A placeholder padded to length is still a placeholder."""
        with pytest.raises(ValidationError):
            make(api_key=weak.ljust(20, weak[0]) if len(weak) < 16 else weak)

    def test_admin_key_is_validated_too(self) -> None:
        with pytest.raises(ValidationError):
            make(admin_api_key="tiny")

    def test_secret_is_not_exposed_by_repr(self) -> None:
        """Settings get logged and dumped; a key must not ride along."""
        settings = make()
        assert "a-sufficiently-long-key-value" not in repr(settings)
        assert settings.api_key.get_secret_value() == "a-sufficiently-long-key-value"


class TestRedisOptionality:
    def test_absent_redis_is_allowed_and_reported(self) -> None:
        """Redis is a derived index, so its absence is degraded, not fatal (D2)."""
        settings = make(redis_url=None)
        assert settings.redis_url is None
        assert settings.redis_enabled is False

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_redis_url_normalises_to_none(self, blank: str) -> None:
        """An empty env var is how 'unset' actually arrives in a container."""
        assert make(redis_url=blank).redis_url is None

    def test_rejects_non_redis_scheme(self) -> None:
        with pytest.raises(ValidationError):
            make(redis_url="http://localhost:6379")


class TestProductionGuards:
    def test_production_requires_database_tls(self) -> None:
        with pytest.raises(ValidationError, match="TLS in production"):
            make(environment="production", database_url="postgresql://u:p@db:5432/leaderboard")

    def test_production_requires_redis_tls(self) -> None:
        with pytest.raises(ValidationError, match="rediss"):
            make(
                environment="production",
                database_url="postgresql://u:p@db:5432/db?sslmode=require",
                redis_url="redis://cache:6379",
            )

    def test_production_accepts_fully_encrypted_config(self) -> None:
        settings = make(
            environment="production",
            database_url="postgresql://u:p@db:25060/db?sslmode=require",
            redis_url="rediss://cache:25061",
        )
        assert settings.is_production is True

    def test_non_production_does_not_require_tls(self) -> None:
        assert make(environment="local", redis_url="redis://localhost:6379").is_production is False


class TestSettingsBounds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("outbox_sweep_interval_s", 0),
            ("outbox_sweep_interval_s", 301),
            ("db_statement_cache_size", -1),
            ("db_pool_size", 0),
            ("max_request_body_bytes", 0),
        ],
    )
    def test_rejects_out_of_range(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            make(**{field: value})

    def test_statement_cache_can_be_zero_for_pgbouncer(self) -> None:
        """0 is the required value behind a transaction-mode pooler, not a bug."""
        assert make(db_statement_cache_size=0).db_statement_cache_size == 0


def test_get_settings_wraps_failure_in_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad environment must produce one actionable startup error."""
    get_settings.cache_clear()
    monkeypatch.setenv("API_KEY", "x")
    try:
        with pytest.raises(ConfigError, match="will not start"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
