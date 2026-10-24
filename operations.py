"""Deployment readiness, private-page indexing controls, and safe response headers."""
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from flask import jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


def register_operations(app):
    @app.get("/ready")
    def ready():
        factory = app.extensions["phase3a_auth"].session_factory
        if not app.extensions["phase3a_auth"].configured or factory is None:
            return jsonify(status="unavailable"), 503
        try:
            config = Config(str(Path(__file__).parent / "alembic.ini"))
            config.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
            expected = ScriptDirectory.from_config(config).get_current_head()
            with factory() as db:
                actual = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                db.execute(text("SELECT id FROM users LIMIT 1"))
            if actual != [expected]:
                return jsonify(status="unavailable"), 503
        except SQLAlchemyError:
            return jsonify(status="unavailable"), 503
        return jsonify(status="ready")

    @app.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        public_paths = {"/", "/pricing", "/features/instagram-scheduling", "/features/instagram-automation", "/privacy", "/terms", "/deletion", "/robots.txt", "/sitemap.xml"}
        staging = os.getenv("SEARCH_INDEXING_ENABLED", "true").lower() != "true"
        if request.path in {"/privacy", "/terms", "/deletion"} and response.status_code == 200:
            from seo import origin
            response.headers["Link"] = f'<{origin() + request.path}>; rel="canonical"'
        if staging or (request.path not in public_paths and not request.path.startswith("/static/")) or response.status_code >= 400:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if request.path not in public_paths and not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response
