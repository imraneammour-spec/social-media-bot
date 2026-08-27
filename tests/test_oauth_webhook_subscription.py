"""Focused fail-closed tests for OAuth account webhook subscriptions."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, InstagramAccount, InstagramCredential, User


class OAuthWebhookSubscriptionTest(unittest.TestCase):
    """Each test protects the server-only per-account subscription boundary."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            owner = User(email="subscription-owner@example.test")
            other_owner = User(email="subscription-other@example.test")
            session.add_all((owner, other_owner))
            session.flush()
            self.account_a = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617459601",
                provider="instagram",
                connection_status="subscription_pending",
            )
            self.account_b = InstagramAccount(
                user_id=other_owner.id,
                instagram_user_id="17841438617459602",
                provider="instagram",
                connection_status="subscription_pending",
            )
            session.add_all((self.account_a, self.account_b))
            session.flush()
            for account, marker in ((self.account_a, "a"), (self.account_b, "b")):
                session.add(InstagramCredential(
                    instagram_account_id=account.id,
                    token_ciphertext=f"ciphertext-{marker}",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                ))

    def tearDown(self):
        self.engine.dispose()

    def _subscribe(self, *, account_id=None, instagram_user_id=None):
        from database.oauth_webhook_subscription import subscribe_oauth_account_webhooks

        with self.Session() as session:
            return subscribe_oauth_account_webhooks(
                session,
                instagram_account_id=account_id or self.account_a.id,
                instagram_user_id=instagram_user_id or self.account_a.instagram_user_id,
                keyring=object(),
            )

    def test_pending_account_subscribes_exactly_comments_and_messages_while_engagement_is_off(self):
        """Dropping a field, using the kill switch, or using a global token must fail this contract."""
        from database import oauth_webhook_subscription as subscriptions

        response = Mock(ok=True)
        response.json.return_value = {"success": True}
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "false"}, clear=False), patch.object(
            subscriptions, "decrypt_access_token", return_value="staged-token-a"
        ), patch.object(subscriptions.requests, "post", return_value=response) as post:
            self.assertTrue(self._subscribe())

        post.assert_called_once_with(
            "https://graph.instagram.com/v25.0/17841438617459601/subscribed_apps",
            headers={"Authorization": "Bearer staged-token-a"},
            params={"subscribed_fields": "comments,messages"},
            timeout=15,
        )

    def test_account_identifier_mismatch_cannot_decrypt_or_subscribe_another_accounts_credential(self):
        """Changing only the routed external ID must never borrow account B's credential."""
        from database import oauth_webhook_subscription as subscriptions
        from database.oauth_webhook_subscription import OAuthWebhookSubscriptionError

        with patch.object(subscriptions, "decrypt_access_token") as decrypt, patch.object(
            subscriptions.requests, "post"
        ) as post:
            with self.assertRaises(OAuthWebhookSubscriptionError) as raised:
                self._subscribe(instagram_user_id=self.account_b.instagram_user_id)

        self.assertEqual(str(raised.exception), "Instagram webhook subscription is unavailable.")
        decrypt.assert_not_called()
        post.assert_not_called()

    def test_missing_either_required_granted_scope_prevents_subscription(self):
        """Using requested rather than actually granted scopes must not permit the request."""
        from database import oauth_webhook_subscription as subscriptions
        from database.oauth_webhook_subscription import OAuthWebhookSubscriptionError

        for scopes in (
            ["instagram_business_basic", "instagram_business_manage_comments"],
            ["instagram_business_basic", "instagram_business_manage_messages"],
        ):
            with self.subTest(granted_scopes=scopes):
                with self.Session.begin() as session:
                    credential = session.query(InstagramCredential).filter_by(
                        instagram_account_id=self.account_a.id
                    ).one()
                    credential.scopes = scopes
                with patch.object(subscriptions, "decrypt_access_token") as decrypt, patch.object(
                    subscriptions.requests, "post"
                ) as post:
                    with self.assertRaises(OAuthWebhookSubscriptionError):
                        self._subscribe()
                decrypt.assert_not_called()
                post.assert_not_called()

    def test_invalid_credential_or_provider_result_is_sanitized_and_never_marks_local_state(self):
        """Expiration, rejection, and timeout must fail closed without response or token leakage."""
        from database import oauth_webhook_subscription as subscriptions
        from database.oauth_webhook_subscription import OAuthWebhookSubscriptionError

        with self.Session.begin() as session:
            credential = session.query(InstagramCredential).filter_by(
                instagram_account_id=self.account_a.id
            ).one()
            credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch.object(subscriptions, "decrypt_access_token") as decrypt, patch.object(
            subscriptions.requests, "post"
        ) as post:
            with self.assertRaises(OAuthWebhookSubscriptionError) as raised:
                self._subscribe()
        self.assertEqual(str(raised.exception), "Instagram webhook subscription is unavailable.")

    def test_revoked_credential_timeout_and_malformed_success_are_sanitized(self):
        """Removing lifecycle checks or accepting an ambiguous response must fail this test."""
        from database import oauth_webhook_subscription as subscriptions
        from database.oauth_webhook_subscription import OAuthWebhookSubscriptionError

        with self.Session.begin() as session:
            credential = session.query(InstagramCredential).filter_by(
                instagram_account_id=self.account_a.id
            ).one()
            credential.revoked_at = datetime.now(timezone.utc)
        with patch.object(subscriptions, "decrypt_access_token") as decrypt, patch.object(
            subscriptions.requests, "post"
        ) as post:
            with self.assertRaises(OAuthWebhookSubscriptionError) as raised:
                self._subscribe()
        self.assertEqual(str(raised.exception), "Instagram webhook subscription is unavailable.")
        decrypt.assert_not_called()
        post.assert_not_called()

        with self.Session.begin() as session:
            credential = session.query(InstagramCredential).filter_by(
                instagram_account_id=self.account_a.id
            ).one()
            credential.revoked_at = None
        for provider_result in (
            subscriptions.requests.Timeout("provider endpoint unavailable"),
            Mock(ok=True, json=Mock(return_value={"success": False})),
        ):
            with self.subTest(result=type(provider_result).__name__), patch.object(
                subscriptions, "decrypt_access_token", return_value="staged-token-a"
            ), patch.object(
                subscriptions.requests, "post",
                side_effect=provider_result if isinstance(provider_result, Exception) else None,
                return_value=None if isinstance(provider_result, Exception) else provider_result,
            ):
                with self.assertRaises(OAuthWebhookSubscriptionError) as raised:
                    self._subscribe()
            self.assertEqual(str(raised.exception), "Instagram webhook subscription is unavailable.")
        decrypt.assert_not_called()
        post.assert_not_called()

        with self.Session.begin() as session:
            credential = session.query(InstagramCredential).filter_by(
                instagram_account_id=self.account_a.id
            ).one()
            credential.expires_at = None
        rejected = Mock(ok=False)
        with patch.object(subscriptions, "decrypt_access_token", return_value="staged-token-a"), patch.object(
            subscriptions.requests, "post", return_value=rejected
        ):
            with self.assertRaises(OAuthWebhookSubscriptionError) as raised:
                self._subscribe()
        self.assertEqual(str(raised.exception), "Instagram webhook subscription is unavailable.")

    def test_repeated_subscription_has_no_local_duplicate_state(self):
        """A later reconnect can repeat the same provider request without creating local records."""
        from database import oauth_webhook_subscription as subscriptions

        response = Mock(ok=True)
        response.json.return_value = {"success": True}
        with patch.object(subscriptions, "decrypt_access_token", return_value="staged-token-a"), patch.object(
            subscriptions.requests, "post", return_value=response
        ) as post:
            self.assertTrue(self._subscribe())
            self.assertTrue(self._subscribe())

        self.assertEqual(post.call_count, 2)
        with self.Session() as session:
            self.assertEqual(session.query(InstagramCredential).count(), 2)


if __name__ == "__main__":
    unittest.main()
