"""Side-effect-free Flask application for explicit database CLI commands."""

from __future__ import annotations

import os

from flask import Flask

from .cli import register_database_commands


def _legacy_instagram_user_id() -> str | None:
    return os.getenv("LEGACY_INSTAGRAM_USER_ID", "").strip() or None


app = Flask(__name__)
register_database_commands(app, _legacy_instagram_user_id)
