"""Lazy database engine construction for explicit commands only."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker


def create_database_engine(database_url: str) -> Engine:
    """Construct an engine without opening a connection or creating tables."""

    return create_engine(database_url, pool_pre_ping=not database_url.startswith("sqlite"))


def create_session_factory(database_url: str) -> sessionmaker:
    return sessionmaker(bind=create_database_engine(database_url), expire_on_commit=False)
