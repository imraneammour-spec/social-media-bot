"""Native staging deployment and leased work across independent DB sessions."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def test_native_fresh_migration_probe_and_lease_recovery(tmp_path):
    from database.models import User, InstagramAccount, InstagramOAuthWebhookWorkItem
    from database.oauth_webhook_ingress import claim_due_oauth_webhook_work, complete_oauth_webhook_work
    from scripts.serv00 import migrate_fresh
    test_url = os.getenv("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL required for real PostgreSQL.")
    original = os.environ.copy()
    admin_url = make_url(test_url)
    assert (admin_url.database or "").endswith("_test")
    name = "serv00_" + uuid.uuid4().hex + "_test"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    target = admin_url.set(database=name)
    engine = None
    try:
        with admin.connect() as db:
            db.execute(text(f'CREATE DATABASE "{name}"'))
        config = tmp_path / "config.json"
        data = tmp_path / "persistent"
        config.write_text(json.dumps({
            "DATABASE_URL": target.render_as_string(hide_password=False),
            "FLASK_SECRET_KEY": "isolated-serv00-staging-test-secret",
            "PUBLIC_BASE_URL": "https://preview.example.test",
            "POSTENZA_DATA_DIR": str(data),
        }), encoding="utf-8")
        config.chmod(0o600)
        assert migrate_fresh(config, name)["railway_recovery"] is False
        engine = create_engine(target)
        tables = inspect(engine).get_table_names()
        # Migration 20261126_02 added five tables (four legacy-state tables
        # plus the per-account Gemini key table); migration 20261127_02 added
        # billing_credit_lots for one-time AI credit packs. Keep the count in
        # step with the migration chain and name the additions so the next
        # table addition fails with a readable message instead of a bare count.
        # Count includes the alembic_version table (25 migration tables + 1).
        assert len(tables) == 26
        assert {"legacy_processed_comments", "legacy_prompt_requests", "legacy_pending_access",
                "legacy_prompt_posts", "user_gemini_keys", "billing_credit_lots"} <= set(tables)
        with engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM users")) == 0
        with pytest.raises(ValueError, match="nonempty"):
            migrate_fresh(config, name)
        sentinel = data / "sentinel"
        sentinel.write_bytes(b"keep-across-cron-restarts")
        for action in ("verify", "tick", "tick"):
            result = subprocess.run([sys.executable, "scripts/serv00.py", action, "--config", str(config)], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, (result.stdout, result.stderr)
        assert (data / "worker-probe-heartbeat").exists()
        assert not (data / "worker-heartbeat").exists()
        assert sentinel.read_bytes() == b"keep-across-cron-restarts"
        factory = sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        with factory.begin() as db:
            assert db.scalar(text("SELECT count(*) FROM users")) == 0
            owner = User(email="serv00-lease@example.test")
            db.add(owner)
            db.flush()
            account = InstagramAccount(user_id=owner.id, instagram_user_id="123456789")
            db.add(account)
            db.flush()
            work = InstagramOAuthWebhookWorkItem(instagram_account_id=account.id,
                instagram_user_id=account.instagram_user_id, provider_event_id="cron-recovery",
                event_type="comment", payload={}, public_base_url="https://preview.example.test")
            db.add(work)
        with factory.begin() as db:
            abandoned = claim_due_oauth_webhook_work(db, now=now)[0]
        # No complete() call: simulate a worker dying with a committed lease.
        with factory.begin() as db:
            assert claim_due_oauth_webhook_work(db, now=now + timedelta(seconds=30)) == []
        later = now + timedelta(hours=1)
        with factory.begin() as db:
            reclaimed = claim_due_oauth_webhook_work(db, now=later)[0]
            assert reclaimed.id == abandoned.id
            assert reclaimed.lease_token != abandoned.lease_token
            assert complete_oauth_webhook_work(db, abandoned.id, abandoned.lease_token, now=later) is False
            assert complete_oauth_webhook_work(db, reclaimed.id, reclaimed.lease_token, now=later) is True
        with factory.begin() as db:
            assert claim_due_oauth_webhook_work(db, now=later) == []
    finally:
        os.environ.clear()
        os.environ.update(original)
        if engine:
            engine.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
