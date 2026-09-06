from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from flask import Flask
from sqlalchemy import select

from auth import (
    action_reference_for_account,
    configure_auth,
    csrf_token,
    register_auth_routes,
)
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    User,
)
from database.tenant_automation import normalize_trigger_keywords
from tenant_dashboard import (
    ACCOUNT_REFERENCE_PURPOSE,
    register_tenant_dashboard,
)


class TestPhase5B1BAutomations(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

        db_path = Path(self.tempdir.name) / "phase5b1b.db"
        self.db_url = f"sqlite+pysqlite:///{db_path}"

        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.app.secret_key = "test-secret"

        configure_auth(
            self.app,
            database_url=self.db_url,
            secret_key=self.app.secret_key,
            legacy_instagram_user_id="legacy-123",
            production=False,
        )

        # The production app registers auth routes before the tenant shell,
        # including its real CSRF-protected POST /logout endpoint.
        register_auth_routes(self.app)
        register_tenant_dashboard(self.app)

        from database.runtime import create_session_factory

        self.Session = create_session_factory(self.db_url)

        self.app.extensions["phase3a_auth"].session_factory = self.Session

        # Keep a direct reference to the SQLAlchemy engine so it can
        # be explicitly disposed before TemporaryDirectory cleanup.
        self.engine = self.Session.kw["bind"]

        Base.metadata.create_all(self.engine)

        now = datetime.now(timezone.utc)

        with self.Session.begin() as session:
            self.user = User(
                id=uuid4(),
                display_name="Test User",
                email="test@example.com",
                password_hash="...",
            )
            session.add(self.user)

            self.account = InstagramAccount(
                id=uuid4(),
                user_id=self.user.id,
                instagram_user_id="ext-123",
                username="test_account",
                provider="instagram",
                connection_status="connected",
                connected_at=now,
            )
            session.add(self.account)

            self.credential = InstagramCredential(
                instagram_account_id=self.account.id,
                token_ciphertext="test-ciphertext",
                encryption_key_id="test-key",
                scopes=[
                    "instagram_business_basic",
                    "instagram_business_manage_comments",
                    "instagram_business_manage_messages",
                ],
                expires_at=now + timedelta(days=30),
                token_status="active",
                issued_at=now,
                last_validated_at=now,
            )
            session.add(self.credential)

            self.config = InstagramAutomationConfig(
                instagram_account_id=self.account.id,
                enabled=True,
                trigger_keywords=["test"],
                public_reply_enabled=True,
                public_reply_text="Reply text",
            )
            session.add(self.config)

        self.client = self.app.test_client()

        with self.client.session_transaction() as sess:
            sess["user_id"] = str(self.user.id)

        self.acc_ref = action_reference_for_account(
            self.user.id,
            self.account.id,
            ACCOUNT_REFERENCE_PURPOSE,
            secret_key=self.app.secret_key,
        )

    def tearDown(self):
        Base.metadata.drop_all(self.engine)

        # Important on Windows:
        # release SQLite file handles before deleting the temp directory.
        self.engine.dispose()

        self.tempdir.cleanup()

    def test_automations_page_access(self):
        """Owner can view own automation settings."""

        response = self.client.get(
            f"/app/automations?account={self.acc_ref}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>Automation</h1>", response.data)
        self.assertIn(b'action="/logout"', response.data)
        self.assertIn(b"test_account", response.data)

    def test_automations_update_own(self):
        """Owner can update only their own automation settings."""

        with self.client:
            self.client.get(
                f"/app/automations?account={self.acc_ref}"
            )

            token = csrf_token()

            response = self.client.post(
                f"/app/automations?account={self.acc_ref}",
                data={
                    "csrf_token": token,
                    "enabled": "on",
                    "delivery_mode": "direct_message",
                    "message_trigger_keywords": "updated, keywords",
                    "message_public_reply_enabled": "on",
                    "message_public_reply_text": "New reply text",
                },
                follow_redirects=True,
            )

            self.assertEqual(response.status_code, 200)

        with self.Session() as session:
            config = session.scalar(
                select(InstagramAutomationConfig).where(
                    InstagramAutomationConfig.instagram_account_id
                    == self.account.id
                )
            )

            self.assertIsNotNone(config)
            self.assertTrue(config.enabled)
            self.assertEqual(config.delivery_mode, "direct_message")

            self.assertEqual(
                config.trigger_keywords,
                ["updated", "keywords"],
            )

            self.assertTrue(config.public_reply_enabled)

            self.assertEqual(
                config.public_reply_text,
                "New reply text",
            )

    def test_cross_owner_ref_blocked(self):
        """Opaque account references from another owner fail closed."""

        other_user_id = uuid4()

        other_acc_ref = action_reference_for_account(
            other_user_id,
            self.account.id,
            ACCOUNT_REFERENCE_PURPOSE,
            secret_key=self.app.secret_key,
        )

        response = self.client.get(
            f"/app/automations?account={other_acc_ref}"
        )

        self.assertEqual(response.status_code, 404)

    def test_validation_keywords(self):
        """Keyword normalization keeps existing single-token semantics."""

        self.assertEqual(
            normalize_trigger_keywords(
                ["Word", " word "]
            ),
            ["word"],
        )

        with self.assertRaises(ValueError):
            normalize_trigger_keywords(
                [f"k{i}" for i in range(11)]
            )

        with self.assertRaises(ValueError):
            normalize_trigger_keywords([])

        with self.assertRaises(ValueError):
            normalize_trigger_keywords(
                ["a" * 33]
            )

        with self.assertRaises(ValueError):
            normalize_trigger_keywords(
                ["bad!word"]
            )

    def test_enabled_config_cannot_have_empty_triggers(self):
        """An enabled automation cannot use an empty trigger list."""

        with self.assertRaises(ValueError):
            normalize_trigger_keywords([])

    def test_public_reply_configuration_round_trip(self):
        """Stored public-reply configuration is account scoped."""

        with self.Session() as session:
            config = session.scalar(
                select(InstagramAutomationConfig).where(
                    InstagramAutomationConfig.instagram_account_id
                    == self.account.id
                )
            )

            self.assertIsNotNone(config)
            self.assertTrue(config.public_reply_enabled)
            self.assertEqual(
                config.public_reply_text,
                "Reply text",
            )


if __name__ == "__main__":
    unittest.main()
