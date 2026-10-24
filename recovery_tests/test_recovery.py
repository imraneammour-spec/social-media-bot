import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from werkzeug.security import generate_password_hash

from database.models import BillingSubscription, InstagramAccount, InstagramCredential, User
from database.credentials import credential_keyring_from_environment, encrypt_access_token, decrypt_access_token
from database.token_refresh import refresh_expiring_tokens
from worker import run_once


def token(client, path="/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def test_public_pages_sitemap_and_canonical(app):
    client = app.test_client()
    xml = ET.fromstring(client.get("/sitemap.xml").data)
    urls = [entry.text for entry in xml.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert "https://postenza.app/pricing" in urls
    for url in urls:
        path = url.removeprefix("https://postenza.app")
        response = client.get(path)
        assert response.status_code == 200
        assert "noindex" not in response.headers.get("X-Robots-Tag", "")
    assert 'rel="canonical" href="https://postenza.app/"' in client.get("/").text
    assert "Sitemap: https://postenza.app/sitemap.xml" in client.get("/robots.txt").text


def test_staging_never_indexed(app, monkeypatch):
    monkeypatch.setenv("SEARCH_INDEXING_ENABLED", "false")
    assert "noindex" in app.test_client().get("/").headers["X-Robots-Tag"]
    assert "Disallow: /\n" in app.test_client().get("/robots.txt").text


def test_legacy_user_without_password_returns_401(app, user):
    client = app.test_client()
    response = client.post("/login", data={"csrf_token": token(client), "email": user.email, "password": "not-the-password"})
    assert response.status_code == 401


def test_registration_session_login_and_logout(app, factory):
    client = app.test_client()
    response = client.post("/register", data={"csrf_token": token(client, "/register"), "display_name":"Test User", "email":"new@example.test", "password":"test-only-password-long"})
    assert response.status_code == 302
    assert "/onboarding" in response.location
    assert client.get("/onboarding").status_code == 200
    with client.session_transaction() as session:
        assert session["user_id"]
        csrf = session["_csrf_token"]
    assert client.post("/logout", data={"csrf_token":csrf}).status_code == 302
    assert client.get("/app/billing").status_code == 302
    response = client.post("/login", data={"csrf_token": token(client), "email":"new@example.test", "password":"test-only-password-long"})
    assert response.status_code == 302
    assert client.get("/app/billing").status_code == 200


def test_disabled_user_cannot_keep_session(client, factory, user):
    with factory.begin() as db:
        db.get(User, user.id).status = "disabled"
    assert client.get("/app/billing").status_code == 302


def test_health_is_live_but_readiness_checks_schema(app):
    assert app.test_client().get("/health").status_code == 200
    assert app.test_client().get("/ready").status_code == 503


def test_worker_failure_does_not_block_next_queue():
    succeeding = Mock()
    run_once([("failing", Mock(side_effect=RuntimeError("secret-must-not-log"))), ("next", succeeding)])
    succeeding.assert_called_once()


def test_billing_migration_preserves_users_and_creates_legacy_access(tmp_path, monkeypatch):
    url = "sqlite:///" + str(tmp_path / "migrate.db")
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "20260907_01")
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (id,email,status,created_at,updated_at) VALUES ('11111111111111111111111111111111','retained@example.test','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    command.upgrade(config, "head")
    with engine.connect() as db:
        assert db.execute(text("SELECT email FROM users")).scalar() == "retained@example.test"
        assert db.execute(text("SELECT plan FROM billing_subscriptions")).scalar() == "legacy"
    command.downgrade(config, "20260907_01")
    assert "billing_subscriptions" not in inspect(engine).get_table_names()
    engine.dispose()


def test_token_refresh_encrypts_and_updates_expiry(factory, user, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID", "TEST")
    monkeypatch.setenv("INSTAGRAM_CREDENTIAL_KEY_TEST", Fernet.generate_key().decode())
    keyring = credential_keyring_from_environment()
    encrypted = encrypt_access_token("old-token", keyring)
    now = datetime.now(timezone.utc)
    with factory.begin() as db:
        account = InstagramAccount(user_id=user.id, instagram_user_id="1234", connection_status="connected", token_expires_at=now+timedelta(days=2))
        db.add(account); db.flush()
        credential = InstagramCredential(instagram_account_id=account.id, token_status="valid", token_ciphertext=encrypted.token_ciphertext,
            encryption_key_id=encrypted.encryption_key_id, issued_at=now-timedelta(days=58), expires_at=now+timedelta(days=2))
        db.add(credential); db.flush(); credential_id = credential.id
    response = Mock()
    response.json.return_value = {"access_token":"new-token", "expires_in":5184000}
    get = Mock(return_value=response)
    assert refresh_expiring_tokens(factory, now=now, get=get) == 1
    with factory() as db:
        updated = db.get(InstagramCredential, credential_id)
        assert decrypt_access_token(updated.token_ciphertext, updated.encryption_key_id, keyring) == "new-token"
        assert updated.token_ciphertext != "new-token"
