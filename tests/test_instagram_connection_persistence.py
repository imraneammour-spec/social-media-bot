import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import SessionTransaction

from database.credentials import credential_keyring_from_environment, load_decrypted_access_token
from database.instagram_connection import (
    InstagramConnectionError,
    complete_discovered_instagram_connection,
    persist_discovered_instagram_connection,
    repair_owned_oauth_connection_identity,
)
from database.oauth_webhook_subscription import (
    OAuthWebhookSubscriptionError,
    WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED,
)
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    InstagramPendingAccess,
    User,
)
from instagram_oauth import (
    InstagramAccountDiscovery,
    InstagramOAuthTokenResult,
    normalized_expires_in,
)


class InstagramConnectionPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'connection.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.keyring = credential_keyring_from_environment(
            {
                "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": Fernet.generate_key().decode("ascii"),
            }
        )
        self.token = "long-lived-token-must-never-persist-in-plaintext"
        with self.Session.begin() as session:
            owner = User(email="connection-owner@example.test")
            other_owner = User(email="connection-other@example.test")
            session.add_all([owner, other_owner])
            session.flush()
            self.owner_id = owner.id
            self.other_owner_id = other_owner.id

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _result(self, *, expires_in=3600, scopes=("instagram_business_basic",)):
        return InstagramOAuthTokenResult(
            long_lived_access_token=self.token,
            expires_in=expires_in,
            scopes=scopes,
        )

    def _discovery(self, *, instagram_user_id="17841438617458003", username="connected_account"):
        return InstagramAccountDiscovery(
            instagram_user_id=instagram_user_id,
            username=username,
        )

    def _accounts(self):
        with self.Session() as session:
            return session.scalars(select(InstagramAccount)).all()

    def _credentials(self):
        with self.Session() as session:
            return session.scalars(select(InstagramCredential)).all()

    def _persist(self, *, user_id=None, discovery=None, result=None, keyring=None, now=None):
        return persist_discovered_instagram_connection(
            self.Session,
            user_id=user_id or self.owner_id,
            legacy_instagram_user_id="17841438617459999",
            discovery=discovery or self._discovery(),
            token_result=result or self._result(),
            keyring=keyring or self.keyring,
            now=now or datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )

    def _complete(self, *, user_id=None, discovery=None, result=None, keyring=None, now=None):
        return complete_discovered_instagram_connection(
            self.Session,
            user_id=user_id or self.owner_id,
            legacy_instagram_user_id="17841438617459999",
            discovery=discovery or self._discovery(),
            token_result=result or self._result(scopes=(
                "instagram_business_basic",
                "instagram_business_manage_comments",
                "instagram_business_manage_messages",
            )),
            keyring=keyring or self.keyring,
            now=now or datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )

    def _repair(self, *, owner_id=None, keyring=None, now=None):
        return repair_owned_oauth_connection_identity(
            self.Session,
            owner_id=owner_id or self.owner_id,
            legacy_instagram_user_id="17841438617459999",
            keyring=keyring or self.keyring,
            now=now or datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )

    def test_new_connection_creates_one_owned_account_and_one_encrypted_credential(self):
        connected = self._persist()

        self.assertEqual(connected.user_id, self.owner_id)
        self.assertEqual(connected.connection_status, "connected")
        self.assertEqual(len(self._accounts()), 1)
        credentials = self._credentials()
        self.assertEqual(len(credentials), 1)
        self.assertNotEqual(credentials[0].token_ciphertext, self.token)
        self.assertNotIn(self.token, credentials[0].token_ciphertext)
        self.assertEqual(credentials[0].token_status, "valid")
        self.assertEqual(credentials[0].scopes, ["instagram_business_basic"])
        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), self.token)

    def test_missing_username_creates_a_safe_account_without_inventing_a_value(self):
        connected = self._persist(discovery=self._discovery(username=None))

        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            self.assertIsNone(account.username)
            self.assertEqual(account.connection_status, "connected")

    def test_same_owner_reconnect_updates_one_account_and_credential_atomically(self):
        first = self._persist()
        replacement = "replacement-long-lived-token-must-remain-private"
        self.token = replacement
        second = self._persist(
            discovery=self._discovery(username="renamed_account"),
            result=self._result(expires_in=7200, scopes=None),
            now=datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(len(self._credentials()), 1)
        with self.Session() as session:
            account = session.get(InstagramAccount, first.id)
            credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == first.id)
            )
            self.assertEqual(account.username, "renamed_account")
            self.assertEqual(
                account.token_expires_at.replace(tzinfo=timezone.utc),
                datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(credential.scopes, None)
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), replacement)

    def test_explicit_owner_scoped_repair_replaces_only_app_scoped_identity(self):
        app_scoped_id = "17841438617458003"
        professional_id = "17841438617458004"
        existing = self._persist(discovery=self._discovery(instagram_user_id=app_scoped_id))
        with self.Session.begin() as session:
            session.add(InstagramPendingAccess(
                instagram_account_id=existing.id,
                recipient_id="recipient-preserved",
                page_url="https://example.test/preserved",
            ))
        with self.Session() as session:
            account = session.get(InstagramAccount, existing.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == existing.id
            ))
            before = (
                account.id,
                account.user_id,
                account.connection_status,
                account.token_expires_at,
                credential.token_ciphertext,
                credential.encryption_key_id,
                list(credential.scopes),
            )
        response = type("ProfileResponse", (), {
            "ok": True,
            "json": lambda _self: {"id": app_scoped_id, "user_id": professional_id},
        })()

        with patch("database.instagram_connection.requests.get", return_value=response) as get:
            repaired = self._repair()

        self.assertEqual(repaired.id, existing.id)
        get.assert_called_once()
        with self.Session() as session:
            account = session.get(InstagramAccount, existing.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == existing.id
            ))
            self.assertEqual(account.instagram_user_id, professional_id)
            self.assertEqual(
                (
                    account.id,
                    account.user_id,
                    account.connection_status,
                    account.token_expires_at,
                    credential.token_ciphertext,
                    credential.encryption_key_id,
                    list(credential.scopes),
                ),
                before,
            )
            self.assertEqual(
                session.scalar(select(InstagramPendingAccess).where(
                    InstagramPendingAccess.instagram_account_id == existing.id
                )).recipient_id,
                "recipient-preserved",
            )
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), self.token)
            from database.account_runtime import resolve_instagram_account_route

            route = resolve_instagram_account_route(
                session,
                instagram_user_id=professional_id,
                legacy_instagram_user_id="17841438617459999",
                now=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(route.instagram_account_id, existing.id)
            self.assertEqual(route.connection_mode, "oauth")

    def test_repair_is_idempotent_when_the_professional_identity_is_already_stored(self):
        professional_id = "17841438617458004"
        existing = self._persist(discovery=self._discovery(instagram_user_id=professional_id))
        response = type("ProfileResponse", (), {
            "ok": True,
            "json": lambda _self: {
                "id": "17841438617458003",
                "user_id": professional_id,
            },
        })()

        with patch("database.instagram_connection.requests.get", return_value=response):
            repaired = self._repair()

        self.assertEqual(repaired.id, existing.id)
        self.assertEqual(repaired.user_id, self.owner_id)
        self.assertEqual(repaired.instagram_user_id, professional_id)
        self.assertEqual(repaired.connection_status, "connected")

    def test_repair_rejects_conflicting_professional_identity_without_mutation(self):
        app_scoped_id = "17841438617458003"
        professional_id = "17841438617458004"
        existing = self._persist(discovery=self._discovery(instagram_user_id=app_scoped_id))
        with self.Session.begin() as session:
            session.add(InstagramAccount(
                user_id=self.other_owner_id,
                instagram_user_id=professional_id,
                provider="instagram",
                connection_status="connected",
            ))
        response = type("ProfileResponse", (), {
            "ok": True,
            "json": lambda _self: {"id": app_scoped_id, "user_id": professional_id},
        })()

        with patch("database.instagram_connection.requests.get", return_value=response):
            with self.assertRaises(InstagramConnectionError):
                self._repair()

        with self.Session() as session:
            self.assertEqual(session.get(InstagramAccount, existing.id).instagram_user_id, app_scoped_id)

    def test_repair_rejects_ambiguous_or_legacy_candidates_before_profile_lookup(self):
        self._persist()
        self._persist(discovery=self._discovery(instagram_user_id="17841438617458004"))
        with patch("database.instagram_connection.requests.get") as get:
            with self.assertRaises(InstagramConnectionError):
                self._repair()
        get.assert_not_called()

        with self.Session.begin() as session:
            session.add(InstagramAccount(
                user_id=self.other_owner_id,
                instagram_user_id="17841438617459999",
                provider="instagram",
                connection_status="environment_managed",
            ))
        with patch("database.instagram_connection.requests.get") as get:
            with self.assertRaises(InstagramConnectionError):
                self._repair(owner_id=self.other_owner_id)
        get.assert_not_called()

    def test_repair_rejects_stale_or_malformed_provider_identity_without_mutation(self):
        app_scoped_id = "17841438617458003"
        professional_id = "17841438617458004"
        existing = self._persist(discovery=self._discovery(instagram_user_id=app_scoped_id))

        def stale_then_respond(*_args, **_kwargs):
            with self.Session.begin() as session:
                credential = session.scalar(select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == existing.id
                ))
                credential.token_ciphertext = "changed-after-profile-read"
            return type("ProfileResponse", (), {
                "ok": True,
                "json": lambda _self: {"id": app_scoped_id, "user_id": professional_id},
            })()

        with patch("database.instagram_connection.requests.get", side_effect=stale_then_respond):
            with self.assertRaises(InstagramConnectionError):
                self._repair()
        with self.Session() as session:
            self.assertEqual(session.get(InstagramAccount, existing.id).instagram_user_id, app_scoped_id)

        with patch("database.instagram_connection.requests.get", return_value=type(
            "MalformedProfileResponse", (), {
                "ok": True,
                "json": lambda _self: {"id": app_scoped_id, "user_id": None},
            }
        )()):
            with self.assertRaises(InstagramConnectionError):
                self._repair()
        with self.Session() as session:
            self.assertEqual(session.get(InstagramAccount, existing.id).instagram_user_id, app_scoped_id)

    def test_repair_database_failure_rolls_back_without_secret_logging(self):
        app_scoped_id = "17841438617458003"
        professional_id = "17841438617458004"
        existing = self._persist(discovery=self._discovery(instagram_user_id=app_scoped_id))
        response = type("ProfileResponse", (), {
            "ok": True,
            "json": lambda _self: {"id": app_scoped_id, "user_id": professional_id},
        })()

        with patch("database.instagram_connection.requests.get", return_value=response), patch(
            "sqlalchemy.orm.session.SessionTransaction.commit",
            side_effect=SQLAlchemyError("database unavailable"),
        ), patch("builtins.print") as log:
            with self.assertRaises(InstagramConnectionError):
                self._repair()

        log.assert_not_called()
        with self.Session() as session:
            self.assertEqual(session.get(InstagramAccount, existing.id).instagram_user_id, app_scoped_id)

    def test_successful_subscription_commits_the_staged_credential_as_connected(self):
        """Removing the finalization step must leave a safe pending state instead of connected."""
        observed = {}

        def subscription_succeeds_while_pending(session, **kwargs):
            account = session.get(InstagramAccount, kwargs["instagram_account_id"])
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == account.id
            ))
            observed["status"] = account.connection_status
            observed["ciphertext"] = credential.token_ciphertext
            return True

        with patch(
            "database.instagram_connection.subscribe_oauth_account_webhooks",
            side_effect=subscription_succeeds_while_pending,
        ):
            connected = self._complete()

        self.assertEqual(connected.connection_status, "connected")
        self.assertEqual(observed["status"], "subscription_pending")
        self.assertNotEqual(observed["ciphertext"], self.token)
        self.assertNotIn(self.token, observed["ciphertext"])
        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == connected.id
            ))
            self.assertEqual(account.connection_status, "connected")
            self.assertIsNone(account.last_connection_error_code)
            self.assertEqual(credential.scopes, [
                "instagram_business_basic",
                "instagram_business_manage_comments",
                "instagram_business_manage_messages",
            ])
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), self.token)

    def test_failed_reconnect_preserves_old_credential_but_blocks_engagement(self):
        """Replacing an old credential before subscription confirmation would destroy recoverability."""
        original = self._persist(result=self._result(scopes=(
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
        )))
        with self.Session() as session:
            original_credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == original.id
            ))
            original_ciphertext = original_credential.token_ciphertext
        self.token = "replacement-token-that-must-not-become-usable"

        from database.oauth_webhook_subscription import (
            WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED,
        )

        with patch(
            "database.instagram_connection.subscribe_oauth_account_webhooks",
            side_effect=OAuthWebhookSubscriptionError(WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED),
        ):
            with self.assertRaises(InstagramConnectionError) as raised:
                self._complete()

        self.assertEqual(str(raised.exception), "Instagram connection could not be completed.")
        with self.Session() as session:
            account = session.get(InstagramAccount, original.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == original.id
            ))
            self.assertEqual(account.connection_status, "reconnect_required")
            self.assertEqual(
                account.last_connection_error_code,
                WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED,
            )
            self.assertEqual(credential.token_ciphertext, original_ciphertext)
            self.assertNotIn(self.token, credential.token_ciphertext)
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring),
                             "long-lived-token-must-never-persist-in-plaintext")

    def test_reconnect_readiness_uses_only_the_scopes_meta_granted(self):
        """Replacing a credential with a denied scope must return to reconnect-required."""
        from database.oauth_capability import assess_oauth_engagement_records

        full_scopes = (
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
        )
        first_now = datetime.now(timezone.utc)
        connected = self._persist(result=self._result(scopes=full_scopes), now=first_now)
        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == connected.id
            ))
            active = assess_oauth_engagement_records(
                account,
                credential,
                environment={"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"},
            )
        self.assertEqual(active.state, "active")

        reconnected = self._persist(
            result=self._result(scopes=(
                "instagram_business_basic",
                "instagram_business_manage_comments",
            )),
            now=first_now + timedelta(minutes=1),
        )
        with self.Session() as session:
            account = session.get(InstagramAccount, reconnected.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == reconnected.id
            ))
            blocked = assess_oauth_engagement_records(
                account,
                credential,
                environment={"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"},
            )
        self.assertEqual(blocked.state, "reconnect_required")

    def test_cross_user_duplicate_and_legacy_account_are_rejected_without_mutation(self):
        existing = self._persist()
        original_credential = self._credentials()[0].token_ciphertext

        with self.assertRaises(InstagramConnectionError) as cross_user:
            self._persist(user_id=self.other_owner_id)
        with self.assertRaises(InstagramConnectionError) as legacy:
            self._persist(
                discovery=self._discovery(instagram_user_id="17841438617459999"),
            )

        self.assertNotIn(existing.instagram_user_id, str(cross_user.exception))
        self.assertEqual(str(cross_user.exception), str(legacy.exception))
        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(self._accounts()[0].user_id, self.owner_id)
        self.assertEqual(self._credentials()[0].token_ciphertext, original_credential)

    def test_invalid_discovery_and_encryption_failure_leave_no_partial_rows(self):
        invalid_discoveries = (
            InstagramAccountDiscovery(instagram_user_id="", username=None),
            InstagramAccountDiscovery(instagram_user_id="not-a-numeric-instagram-id", username=None),
        )
        for discovery in invalid_discoveries:
            with self.assertRaises(InstagramConnectionError):
                self._persist(discovery=discovery)
        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

        with self.assertRaises(InstagramConnectionError):
            self._persist(keyring=object())
        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

    def test_expiry_accepts_only_bounded_positive_integer_input(self):
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        connected = self._persist(result=self._result(expires_in=3600), now=now)
        self.assertEqual(connected.token_expires_at, now + timedelta(hours=1))

        for value in (None, "3600", True, 0, -1, 10**20):
            self.assertIsNone(normalized_expires_in(value))

    def test_database_flush_failure_rolls_back_new_connection(self):
        with patch("database.credentials.Session.flush", side_effect=SQLAlchemyError("storage unavailable")):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

    def test_database_commit_failure_preserves_existing_credential(self):
        existing = self._persist()
        with self.Session() as session:
            original_account = session.get(InstagramAccount, existing.id)
            original_credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == existing.id)
            )
            original_account_state = (
                original_account.connection_status,
                original_account.connected_at,
                original_account.disconnected_at,
                original_account.token_expires_at,
            )
            original_credential_state = (
                original_credential.token_ciphertext,
                original_credential.encryption_key_id,
                original_credential.token_status,
                original_credential.expires_at,
                original_credential.scopes,
            )
        self.token = "replacement-token-that-must-not-commit-on-failure"

        with patch.object(SessionTransaction, "commit", side_effect=SQLAlchemyError("commit unavailable")):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(len(self._credentials()), 1)
        with self.Session() as session:
            account = session.get(InstagramAccount, existing.id)
            credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == existing.id)
            )
            self.assertEqual(
                (
                    account.connection_status,
                    account.connected_at,
                    account.disconnected_at,
                    account.token_expires_at,
                ),
                original_account_state,
            )
            self.assertEqual(
                (
                    credential.token_ciphertext,
                    credential.encryption_key_id,
                    credential.token_status,
                    credential.expires_at,
                    credential.scopes,
                ),
                original_credential_state,
            )

    def test_unique_constraint_race_leaves_no_partial_account_or_credential(self):
        with patch.object(
            SessionTransaction,
            "commit",
            side_effect=IntegrityError("INSERT", {}, Exception("unique constraint")),
        ):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

    def test_successful_subscribed_connection_creates_current_behavior_config(self):
        """Omitting initial config after subscription must fail this rollout test."""

        with patch("database.instagram_connection.subscribe_oauth_account_webhooks", return_value=True):
            connected = self._complete()

        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == connected.id
            ))
        self.assertIsNotNone(config)
        self.assertTrue(config.enabled)
        self.assertEqual(
            config.trigger_keywords,
            ["access", "ai", "prompt", "prompts", "send"],
        )
        self.assertTrue(config.public_reply_enabled)
        self.assertEqual(config.public_reply_text, "Your prompts are ready. Check your messages.")

    def test_failed_subscription_does_not_create_config_for_reconnect_required_account(self):
        """Creating config before subscription confirmation must fail this safety test."""

        with patch(
            "database.instagram_connection.subscribe_oauth_account_webhooks",
            side_effect=OAuthWebhookSubscriptionError(WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED),
        ):
            with self.assertRaises(InstagramConnectionError):
                self._complete()

        with self.Session() as session:
            self.assertEqual(session.scalars(select(InstagramAutomationConfig)).all(), [])

    def test_reconnect_and_direct_compatibility_paths_preserve_one_existing_config(self):
        """Recreating or overwriting tenant choices during persistence must fail."""

        with patch("database.instagram_connection.subscribe_oauth_account_webhooks", return_value=True):
            connected = self._complete()
        with self.Session.begin() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == connected.id
            ))
            config.enabled = False
            config.trigger_keywords = ["custom"]
            config.public_reply_enabled = False
            config.public_reply_text = "Custom tenant copy"

        with patch("database.instagram_connection.subscribe_oauth_account_webhooks", return_value=True):
            self._complete()
        self._persist()

        with self.Session() as session:
            configs = session.scalars(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == connected.id
            )).all()
        self.assertEqual(len(configs), 1)
        self.assertFalse(configs[0].enabled)
        self.assertEqual(configs[0].trigger_keywords, ["custom"])
        self.assertFalse(configs[0].public_reply_enabled)
        self.assertEqual(configs[0].public_reply_text, "Custom tenant copy")


if __name__ == "__main__":
    unittest.main()
