"""Database configuration helpers that never require a database at import time."""

from __future__ import annotations

import os
from collections.abc import Mapping


DATABASE_URL_ENV = "DATABASE_URL"


def database_url_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return an optional SQLAlchemy URL without exposing its secret content.

    Railway commonly supplies ``postgres://`` or ``postgresql://`` URLs.  The
    application standardizes both onto SQLAlchemy's psycopg 3 dialect.  A
    missing or blank value is intentionally valid: Phase 0 continues using
    local JSON files with no database connection attempt.
    """

    source = os.environ if environ is None else environ
    raw_url = str(source.get(DATABASE_URL_ENV, "") or "").strip()
    if not raw_url:
        return None

    if raw_url.startswith("postgres://"):
        return "postgresql+psycopg://" + raw_url.removeprefix("postgres://")
    if raw_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw_url.removeprefix("postgresql://")
    return raw_url
