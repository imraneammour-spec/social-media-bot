"""Flask CLI commands for explicit database operations."""

from __future__ import annotations

import os
from collections.abc import Callable

import click
from flask import Flask

from .bootstrap import BootstrapConfigurationError, bootstrap_current_owner
from .config import database_url_from_environment
from .runtime import create_session_factory


def register_database_commands(
    app: Flask, instagram_user_id_provider: Callable[[], str | None]
) -> None:
    """Register commands without creating an engine or touching PostgreSQL."""

    @app.cli.command("bootstrap-owner")
    @click.option("--email", default=None, help="Owner email; required unless BOOTSTRAP_OWNER_EMAIL is set.")
    @click.option("--name", default=None, help="Optional owner display name.")
    @click.option("--instagram-username", default=None, help="Optional Instagram username.")
    def bootstrap_owner(email: str | None, name: str | None, instagram_username: str | None) -> None:
        """Create the first owner/account mapping after Alembic has run."""

        database_url = database_url_from_environment()
        if database_url is None:
            raise click.ClickException("DATABASE_URL is required for bootstrap-owner.")

        owner_email = email or os.getenv("BOOTSTRAP_OWNER_EMAIL")
        owner_name = name if name is not None else os.getenv("BOOTSTRAP_OWNER_NAME")
        username = (
            instagram_username
            if instagram_username is not None
            else os.getenv("BOOTSTRAP_INSTAGRAM_USERNAME")
        )
        session_factory = create_session_factory(database_url)
        try:
            with session_factory.begin() as session:
                result = bootstrap_current_owner(
                    session,
                    owner_email=owner_email or "",
                    owner_name=owner_name,
                    instagram_user_id=instagram_user_id_provider() or "",
                    instagram_username=username,
                )
        except BootstrapConfigurationError as error:
            raise click.ClickException(str(error)) from error

        click.echo(
            "Bootstrap complete: "
            f"user_created={result.user_created}, account_created={result.account_created}."
        )
