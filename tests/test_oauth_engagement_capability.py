import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, InstagramAccount, InstagramCredential, User


class OAuthEngagementCapabilityTest(unittest.TestCase):
    """Behavioral coverage for the Phase 5D fail-closed engagement boundary."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            owner = User(email="capability-owner@example.test")
            other_owner = User(email="capability-other@example.test")
            session.add_all([owner, other_owner])
            session.flush()
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617459401",
                provider="instagram",
                connection_status="connected",
            )
            self.other_account = InstagramAccount(
                user_id=other_owner.id,
                instagram_user_id="17841438617459402",
                provider="instagram",
                connection_status="connected",
            )
            session.add_all([self.account, self.other_account])
            session.flush()
            self.account_id = self.account.id
            self.other_account_id = self.other_account.id
            session.add_all([
                InstagramCredential(
                    instagram_account_id=self.account_id,
                    token_ciphertext="ciphertext-a",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=["instagram_business_basic"],
                ),
                InstagramCredential(
                    instagram_account_id=self.other_account_id,
                    token_ciphertext="ciphertext-b",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                ),
            ])

    def tearDown(self):
        self.engine.dispose()

    def _assessment(self, *, enabled=None):
        from database.oauth_capability import assess_oauth_engagement_capability

        environment = {} if enabled is None else {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": enabled}
        with patch.dict(os.environ, environment, clear=False):
            with self.Session() as session:
                return assess_oauth_engagement_capability(
                    session,
                    instagram_account_id=self.account_id,
                    instagram_user_id="17841438617459401",
                )

    def test_basic_only_credential_requires_reconnect_even_when_switch_is_enabled(self):
        """Removing a required granted scope must block all engagement sends."""
        assessment = self._assessment(enabled="true")

        self.assertEqual(assessment.state, "reconnect_required")
        self.assertFalse(assessment.is_active)

    def test_required_scopes_are_ready_but_disabled_when_switch_is_missing_false_or_malformed(self):
        """A permissive switch parser would accidentally activate OAuth engagement."""
        from database.oauth_capability import assess_oauth_engagement_capability

        for value in (None, "", "false", "1", "yes", "enabled"):
            with self.subTest(value=value), self.Session() as session:
                with self.Session.begin() as write_session:
                    credential = write_session.query(InstagramCredential).filter_by(
                        instagram_account_id=self.account_id
                    ).one()
                    credential.scopes = [
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ]
                environment = {} if value is None else {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": value}
                with patch.dict(os.environ, environment, clear=False):
                    assessment = assess_oauth_engagement_capability(
                        session,
                        instagram_account_id=self.account_id,
                        instagram_user_id="17841438617459401",
                    )
                self.assertEqual(assessment.state, "ready_for_activation")
                self.assertFalse(assessment.is_active)

    def test_required_scopes_and_explicit_true_switch_are_active(self):
        """Only an explicit true value may enable a fully usable account."""
        with self.Session.begin() as session:
            credential = session.query(InstagramCredential).filter_by(
                instagram_account_id=self.account_id
            ).one()
            credential.scopes = [
                "instagram_business_basic",
                "instagram_business_manage_comments",
                "instagram_business_manage_messages",
            ]

        assessment = self._assessment(enabled="true")

        self.assertEqual(assessment.state, "active")
        self.assertTrue(assessment.is_active)

    def test_missing_each_required_scope_is_reconnect_required(self):
        """Either engagement permission omitted by Meta must fail closed."""
        for scopes in (
            ["instagram_business_basic", "instagram_business_manage_messages"],
            ["instagram_business_basic", "instagram_business_manage_comments"],
        ):
            with self.subTest(scopes=scopes), self.Session.begin() as session:
                credential = session.query(InstagramCredential).filter_by(
                    instagram_account_id=self.account_id
                ).one()
                credential.scopes = scopes
            assessment = self._assessment(enabled="true")
            self.assertEqual(assessment.state, "reconnect_required")

    def test_expired_revoked_disconnected_or_missing_material_is_reconnect_required(self):
        """Lifecycle and credential-material regressions must never produce active capability."""
        now = datetime.now(timezone.utc)
        mutations = (
            ("expired", lambda account, credential: setattr(credential, "expires_at", now - timedelta(seconds=1))),
            ("revoked", lambda account, credential: setattr(credential, "revoked_at", now)),
            ("disconnected", lambda account, credential: setattr(account, "connection_status", "disconnected")),
            ("missing-material", lambda account, credential: setattr(credential, "token_ciphertext", None)),
            ("missing-key-id", lambda account, credential: setattr(credential, "encryption_key_id", None)),
        )
        for _name, mutate in mutations:
            with self.subTest(state=_name), self.Session.begin() as session:
                account = session.get(InstagramAccount, self.account_id)
                credential = session.query(InstagramCredential).filter_by(
                    instagram_account_id=self.account_id
                ).one()
                account.connection_status = "connected"
                credential.token_status = "valid"
                credential.revoked_at = None
                credential.expires_at = None
                credential.token_ciphertext = "ciphertext-a"
                credential.encryption_key_id = "KEY_2026_01"
                credential.scopes = [
                    "instagram_business_basic",
                    "instagram_business_manage_comments",
                    "instagram_business_manage_messages",
                ]
                mutate(account, credential)
            assessment = self._assessment(enabled="true")
            self.assertEqual(assessment.state, "reconnect_required")

    def test_account_identifier_mismatch_cannot_use_another_accounts_credential(self):
        """Changing the routed external ID must not borrow a different account credential."""
        from database.oauth_capability import assess_oauth_engagement_capability

        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False):
            with self.Session() as session:
                assessment = assess_oauth_engagement_capability(
                    session,
                    instagram_account_id=self.account_id,
                    instagram_user_id="17841438617459402",
                )

        self.assertEqual(assessment.state, "reconnect_required")
        self.assertFalse(assessment.is_active)


if __name__ == "__main__":
    unittest.main()
