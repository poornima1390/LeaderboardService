"""Driver connect arguments, especially the TLS mapping.

Regression coverage for a real deployment failure: ``sslmode=require`` was
mapped to asyncpg's ``ssl=True``, which is not the same thing. libpq's
``require`` means *encrypt without verifying the certificate*; asyncpg's
``True`` means *encrypt and fully verify*. The service therefore failed its
health checks with SSLCertVerificationError against DigitalOcean Managed
Postgres, whose CA is not in the container trust store — while the Alembic job
connected happily, because it set no ``ssl`` argument at all.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.db import asyncpg_connect_args

BASE = {
    "environment": "local",
    "api_key": "a-sufficiently-long-key-value",
    "admin_api_key": "another-sufficiently-long-key",
}


def settings_for(database_url: str, **overrides: object) -> Settings:
    return Settings(**{**BASE, "database_url": database_url, **overrides})  # type: ignore[arg-type]


class TestTlsMapping:
    @pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "prefer", "allow"])
    def test_sslmode_is_passed_through_verbatim(self, mode: str) -> None:
        """The libpq mode string reaches asyncpg unchanged.

        asyncpg implements libpq's semantics for these strings, so passing the
        mode through is both simpler and more faithful than translating it.
        """
        args = asyncpg_connect_args(settings_for(f"postgresql://u:p@db:5432/x?sslmode={mode}"))
        assert args["ssl"] == mode

    def test_require_is_not_translated_to_true(self) -> None:
        """The exact bug. `ssl=True` means verify-full, which `require` is not."""
        args = asyncpg_connect_args(settings_for("postgresql://u:p@db:5432/x?sslmode=require"))
        assert args["ssl"] is not True
        assert args["ssl"] == "require"

    def test_no_ssl_argument_when_tls_not_requested(self) -> None:
        args = asyncpg_connect_args(settings_for("postgresql://u:p@db:5432/x"))
        assert "ssl" not in args

    def test_sslmode_disable_requests_no_tls(self) -> None:
        args = asyncpg_connect_args(settings_for("postgresql://u:p@db:5432/x?sslmode=disable"))
        assert "ssl" not in args

    def test_digitalocean_style_url_produces_a_usable_config(self) -> None:
        """The exact URL shape DigitalOcean hands out."""
        settings = settings_for(
            "postgresql://doadmin:pw@db-postgresql-nyc3-1234-do-user-1-0."
            "l.db.ondigitalocean.com:25060/defaultdb?sslmode=require"
        )
        assert settings.database_url.startswith("postgresql+asyncpg://")
        assert "sslmode" not in settings.database_url
        assert asyncpg_connect_args(settings)["ssl"] == "require"


class TestPoolerCompatibility:
    def test_statement_cache_is_always_set(self) -> None:
        """asyncpg caches prepared statements by default, which breaks behind
        a transaction-mode pooler. The value must be explicit, never implicit."""
        args = asyncpg_connect_args(settings_for("postgresql://u:p@db:5432/x"))
        assert args["statement_cache_size"] == 100

    def test_statement_cache_can_be_disabled_for_pgbouncer(self) -> None:
        args = asyncpg_connect_args(
            settings_for("postgresql://u:p@db:5432/x", db_statement_cache_size=0)
        )
        assert args["statement_cache_size"] == 0

    def test_connect_timeout_is_bounded(self) -> None:
        """An unbounded connect blocks a worker indefinitely on a dead host."""
        args = asyncpg_connect_args(settings_for("postgresql://u:p@db:5432/x"))
        assert 0 < args["timeout"] <= 30
