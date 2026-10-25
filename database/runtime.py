"""Lazy database engine construction for explicit commands only."""

from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Construct an engine without opening a connection or creating tables."""

    options = {"pool_pre_ping": not database_url.startswith("sqlite")}
    if database_url.startswith(("postgresql", "postgres:")):
        options.update(pool_size=5, max_overflow=5, pool_timeout=10, pool_recycle=300,
                       connect_args={"connect_timeout": 10})
        if os.getenv("POSTENZA_HOST_PROFILE") == "serv00":
            options.update(pool_size=1, max_overflow=0)
    return create_engine(database_url, **options)


def create_session_factory(database_url: str) -> sessionmaker:
    return sessionmaker(bind=create_database_engine(database_url), expire_on_commit=False)
