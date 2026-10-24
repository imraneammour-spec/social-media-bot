import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ["POSTENZA_EMBEDDED_SCHEDULER"] = "false"
os.environ["POSTENZA_DATA_DIR"] = tempfile.mkdtemp(prefix="postenza-test-")
os.environ["FLASK_SECRET_KEY"] = "isolated-test-secret"
os.environ["GEMINI_API_KEY"] = ""
os.environ["INSTAGRAM_ACCESS_TOKEN"] = ""
os.environ["APP_ENV"] = "test"

from auth import configure_auth
from database.models import Base, User
from webhook import app as application


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "test")
    monkeypatch.setenv("LEMON_API_KEY", "test-only-key")
    monkeypatch.setenv("LEMON_WEBHOOK_SECRET", "test-only-signing-secret")
    monkeypatch.setenv("LEMON_STORE_ID", "1")
    monkeypatch.setenv("LEMON_PRO_MONTHLY_VARIANT_ID", "10")
    monkeypatch.setenv("LEMON_AGENCY_ANNUAL_VARIANT_ID", "20")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://postenza.app")
    monkeypatch.setattr("webhook.PUBLIC_BASE_URL", "https://postenza.app")
    monkeypatch.setenv("SEARCH_INDEXING_ENABLED", "true")
    url = "sqlite:///" + str(tmp_path / "test.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    configure_auth(application, database_url=url, secret_key="isolated-test-secret", legacy_instagram_user_id="", production=False)
    application.config.update(TESTING=True)
    yield application
    application.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
    engine.dispose()


@pytest.fixture
def factory(app):
    return app.extensions["phase3a_auth"].session_factory


@pytest.fixture
def user(factory):
    with factory.begin() as db:
        user = User(email="owner@example.test", display_name="Owner")
        db.add(user)
        db.flush()
    return user


@pytest.fixture
def client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = str(user.id)
        session["_csrf_token"] = "test-csrf-token-with-enough-characters"
    return client
