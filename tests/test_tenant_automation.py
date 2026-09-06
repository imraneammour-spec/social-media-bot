"""Tests for tenant-owned OAuth automation configuration storage."""

from __future__ import annotations

import importlib
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database import models
from database.models import Base, InstagramAccount, InstagramAutomationConfig, InstagramCredential, User


class TenantAutomationModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp_dir.name}/automation.db")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _config_model(self):
        config_model = getattr(models, "InstagramAutomationConfig", None)
        self.assertIsNotNone(config_model, "tenant automation configuration model is required")
        return config_model

    def _account_id(self):
        with self.Session.begin() as session:
            owner = User(email="automation-owner@example.test")
            session.add(owner)
            session.flush()
            account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458111",
                provider="instagram",
                connection_status="connected",
            )
            session.add(account)
            session.flush()
            return account.id

    def test_configuration_model_has_fail_closed_database_defaults(self):
        """Changing raw creation defaults to active behavior must fail this test."""

        config_model = self._config_model()
        self.assertIn("instagram_automation_configs", Base.metadata.tables)
        account_id = self._account_id()
        with self.Session.begin() as session:
            config = config_model(instagram_account_id=account_id)
            session.add(config)
            session.flush()
            self.assertFalse(config.enabled)
            self.assertEqual(config.trigger_keywords, [])
            self.assertFalse(config.public_reply_enabled)
            self.assertEqual(config.public_reply_text, "")
            self.assertTrue(config.followup_enabled)
            self.assertEqual(config.followup_delay_minutes, 360)
            self.assertFalse(config.smart_reply_enabled)
            self.assertEqual(config.smart_reply_tone, "friendly")
            self.assertEqual(config.smart_reply_max_per_hour, 20)
            self.assertEqual(config.delivery_mode, "prompt_request")
            self.assertEqual(config.private_message_text, "")
            self.assertEqual(config.followup_message_text, "")
            self.assertIsNone(config.prompt_opening_message_text)
            self.assertIsNone(config.destination_url)
            self.assertIsNone(config.inactive_mode_settings)

    def test_configuration_model_allows_only_one_row_per_account(self):
        """Removing the per-account uniqueness guard must fail this test."""

        config_model = self._config_model()
        account_id = self._account_id()
        with self.Session.begin() as session:
            session.add(config_model(instagram_account_id=account_id))
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as session:
                session.add(config_model(instagram_account_id=account_id))


class TenantAutomationHelperTests(unittest.TestCase):
    def _helpers(self):
        spec = importlib.util.find_spec("database.tenant_automation")
        self.assertIsNotNone(spec, "tenant automation helper module is required")
        return importlib.import_module("database.tenant_automation")

    def test_keyword_normalization_preserves_current_single_token_matching(self):
        """Removing lowercase deduplication would change the future matching contract."""

        helpers = self._helpers()
        self.assertEqual(
            helpers.normalize_trigger_keywords([" AI ", "prompt", "ai", "SEND"]),
            ["ai", "prompt", "send"],
        )
        self.assertEqual(
            helpers.current_default_trigger_keywords(),
            ["send", "info"],
        )

    def test_keyword_normalization_rejects_malformed_or_phrase_values(self):
        """Accepting empty, phrase, oversized, or non-list input must fail this test."""

        helpers = self._helpers()
        for value in (None, [], ["two words"], ["x" * 33], ["ai"] * 11):
            with self.assertRaises(helpers.TenantAutomationConfigurationError):
                helpers.normalize_trigger_keywords(value)

    def test_link_delivery_mode_and_destination_urls_are_strictly_validated(self):
        """Accepting unsafe/malformed URLs or rejecting HTTP(S) would break Link delivery."""

        helpers = self._helpers()
        self.assertEqual(helpers.normalize_automation_delivery_mode("Link"), "link")
        self.assertEqual(
            helpers.normalize_destination_url(" https://example.test/download?id=1 "),
            "https://example.test/download?id=1",
        )
        self.assertEqual(
            helpers.normalize_destination_url("http://example.test/resource"),
            "http://example.test/resource",
        )
        for value in (
            None,
            "",
            "example.test/file",
            "https://",
            "https:///missing-host",
            "https://exa mple.test/file",
            "javascript:alert(1)",
            "data:text/plain,hello",
            "file:///tmp/file",
        ):
            with self.subTest(value=value):
                with self.assertRaises(helpers.TenantAutomationConfigurationError):
                    helpers.normalize_destination_url(value)

    def test_customer_mode_defaults_are_complete_and_mode_specific(self):
        """New Message and Link configurations must receive their own polished copy."""

        helpers = self._helpers()
        self.assertEqual(
            helpers.automation_mode_defaults("direct_message"),
            {
                "trigger_keywords": ["send", "info"],
                "public_reply_enabled": True,
                "public_reply_text": "Thanks for commenting 🙌 I’ve sent you a DM.",
                "private_message_text": "Hey 👋 Thanks for your interest! I’ve got something for you.",
                "destination_url": "",
                "followup_enabled": True,
                "followup_delay_minutes": 360,
                "followup_message_text": "Just checking in 👋 Did you get a chance to see my message?",
            },
        )
        self.assertEqual(
            helpers.automation_mode_defaults("link"),
            {
                "trigger_keywords": ["link", "send"],
                "public_reply_enabled": True,
                "public_reply_text": "Thanks 🙌 I’ve sent the link to your DMs.",
                "private_message_text": "Hey 👋 Thanks for your interest! Here’s the link you asked for.",
                "destination_url": "",
                "followup_enabled": True,
                "followup_delay_minutes": 360,
                "followup_message_text": "Hope you found the link useful 😊 Let me know if you have any questions.",
            },
        )
        prompt_defaults = helpers.automation_mode_defaults("prompt_request")
        self.assertEqual(
            prompt_defaults["public_reply_text"],
            "Thanks 🙌 I’ve sent the prompt to your DMs.",
        )
        self.assertEqual(
            prompt_defaults["private_message_text"],
            "Hey 👋 Thanks for your interest! Your prompt is ready. Tap below and I’ll send it to you.",
        )

    def test_inactive_mode_settings_are_allowlisted_versioned_and_restore_saved_values(self):
        """Snapshots must preserve customer copy without retaining credentials or provider data."""

        helpers = self._helpers()
        stored = {
            "version": 1,
            "modes": {
                "direct_message": {
                    "trigger_keywords": ["custom"],
                    "public_reply_enabled": False,
                    "public_reply_text": "My public reply",
                    "private_message_text": "My private reply",
                    "destination_url": "",
                    "followup_enabled": False,
                    "followup_delay_minutes": 75,
                    "followup_message_text": "My follow-up",
                    "access_token": "must-not-survive",
                    "provider_payload": {"secret": "must-not-survive"},
                },
                "unknown_mode": {"private_message_text": "ignored"},
            },
            "oauth_token": "must-not-survive",
        }

        self.assertEqual(
            helpers.normalize_inactive_mode_settings(stored),
            {
                "version": 1,
                "modes": {
                    "direct_message": {
                        "trigger_keywords": ["custom"],
                        "public_reply_enabled": False,
                        "public_reply_text": "My public reply",
                        "private_message_text": "My private reply",
                        "destination_url": "",
                        "followup_enabled": False,
                        "followup_delay_minutes": 75,
                        "followup_message_text": "My follow-up",
                    }
                },
            },
        )

    def test_mode_restore_precedence_keeps_nonempty_saved_customer_values(self):
        """Inactive saved values must beat active values and defaults without cross-mode bleed."""

        helpers = self._helpers()
        config = InstagramAutomationConfig(
            delivery_mode="direct_message",
            trigger_keywords=["active"],
            public_reply_enabled=False,
            public_reply_text="Active public",
            private_message_text="Active private",
            followup_enabled=False,
            followup_delay_minutes=90,
            followup_message_text="Active follow-up",
            inactive_mode_settings={
                "version": 1,
                "modes": {
                    "link": {
                        "trigger_keywords": ["saved-link"],
                        "public_reply_enabled": False,
                        "public_reply_text": "Saved Link public",
                        "private_message_text": "Saved Link opening",
                        "destination_url": "https://example.test/saved",
                        "followup_enabled": False,
                        "followup_delay_minutes": 45,
                        "followup_message_text": "Saved Link follow-up",
                    }
                },
            },
        )

        self.assertEqual(
            helpers.automation_mode_values(config, "direct_message"),
            {
                "trigger_keywords": ["active"],
                "public_reply_enabled": False,
                "public_reply_text": "Active public",
                "private_message_text": "Active private",
                "destination_url": "",
                "followup_enabled": False,
                "followup_delay_minutes": 90,
                "followup_message_text": "Active follow-up",
            },
        )
        self.assertEqual(
            helpers.automation_mode_values(config, "link")["private_message_text"],
            "Saved Link opening",
        )
        self.assertEqual(
            helpers.automation_mode_values(config, "link")["destination_url"],
            "https://example.test/saved",
        )
        config.inactive_mode_settings = {
            "version": 1,
            "modes": {
                "direct_message": {
                    "trigger_keywords": ["inactivewins"],
                    "public_reply_text": "Inactive saved public",
                    "private_message_text": "Inactive saved private",
                }
            },
        }
        restored = helpers.automation_mode_values(config, "direct_message")
        self.assertEqual(restored["trigger_keywords"], ["inactivewins"])
        self.assertEqual(restored["public_reply_text"], "Inactive saved public")
        self.assertEqual(restored["private_message_text"], "Inactive saved private")

    def test_smart_reply_controls_are_bounded_and_fail_closed(self):
        """Invalid tone or rate input must not become an account runtime choice."""

        helpers = self._helpers()
        self.assertEqual(helpers.normalize_smart_reply_tone("Professional"), "professional")
        self.assertEqual(helpers.normalize_smart_reply_max_per_hour("100"), 100)
        self.assertEqual(helpers.normalize_followup_delay_minutes("360"), 360)
        for tone in (None, "", "playful", "friendly " * 20):
            with self.assertRaises(helpers.TenantAutomationConfigurationError):
                helpers.normalize_smart_reply_tone(tone)
        for limit in (None, "", "0", "101", "many", True):
            with self.assertRaises(helpers.TenantAutomationConfigurationError):
                helpers.normalize_smart_reply_max_per_hour(limit)
        for delay in (None, "", "4", "10081", "many", True):
            with self.assertRaises(helpers.TenantAutomationConfigurationError):
                helpers.normalize_followup_delay_minutes(delay)

    def test_runtime_snapshot_keeps_prompt_and_smart_reply_settings_account_scoped(self):
        """A runtime read must not mix tenant A's smart settings into tenant B."""

        helpers = self._helpers()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        try:
            with Session.begin() as session:
                owner = User(email="runtime-owner@example.test")
                session.add(owner)
                session.flush()
                account_a = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458131",
                    provider="instagram",
                    connection_status="connected",
                )
                account_b = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458132",
                    provider="instagram",
                    connection_status="connected",
                )
                session.add_all([account_a, account_b])
                session.flush()
                session.add_all([
                    InstagramAutomationConfig(
                        instagram_account_id=account_a.id,
                        enabled=True,
                        trigger_keywords=["prompt"],
                        public_reply_enabled=False,
                        public_reply_text="",
                        followup_enabled=True,
                        followup_delay_minutes=60,
                        smart_reply_enabled=True,
                        smart_reply_tone="professional",
                        smart_reply_max_per_hour=7,
                    ),
                    InstagramAutomationConfig(
                        instagram_account_id=account_b.id,
                        enabled=False,
                        trigger_keywords=["access"],
                        public_reply_enabled=False,
                        public_reply_text="",
                        followup_enabled=False,
                        followup_delay_minutes=300,
                        smart_reply_enabled=False,
                        smart_reply_tone="concise",
                        smart_reply_max_per_hour=3,
                    ),
                ])
                account_a_id, account_b_id = account_a.id, account_b.id
            with Session() as session:
                runtime_a = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=account_a_id
                )
                runtime_b = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=account_b_id
                )
        finally:
            engine.dispose()

        self.assertTrue(runtime_a.smart_reply_enabled)
        self.assertEqual(runtime_a.smart_reply_tone, "professional")
        self.assertEqual(runtime_a.smart_reply_max_per_hour, 7)
        self.assertTrue(runtime_a.followup_enabled)
        self.assertEqual(runtime_a.followup_delay_minutes, 60)
        self.assertFalse(runtime_b.enabled)
        self.assertFalse(runtime_b.smart_reply_enabled)
        self.assertEqual(runtime_b.smart_reply_tone, "concise")
        self.assertEqual(runtime_b.smart_reply_max_per_hour, 3)
        self.assertFalse(runtime_b.followup_enabled)
        self.assertEqual(runtime_b.followup_delay_minutes, 300)

    def test_runtime_snapshot_exposes_explicit_general_delivery_settings(self):
        """Direct-message settings must remain isolated and require explicit text."""

        helpers = self._helpers()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        try:
            with Session.begin() as session:
                owner = User(email="direct-runtime-owner@example.test")
                session.add(owner)
                session.flush()
                direct_account = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458141",
                    provider="instagram",
                    connection_status="connected",
                )
                prompt_account = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458142",
                    provider="instagram",
                    connection_status="connected",
                )
                session.add_all([direct_account, prompt_account])
                session.flush()
                session.add_all([
                    InstagramAutomationConfig(
                        instagram_account_id=direct_account.id,
                        enabled=True,
                        trigger_keywords=["hello"],
                        public_reply_enabled=False,
                        public_reply_text="",
                        delivery_mode="direct_message",
                        private_message_text="Configured private reply",
                        followup_enabled=True,
                        followup_delay_minutes=60,
                        followup_message_text="Configured follow-up",
                    ),
                    InstagramAutomationConfig(
                        instagram_account_id=prompt_account.id,
                        enabled=True,
                        trigger_keywords=["prompt"],
                        public_reply_enabled=False,
                        public_reply_text="",
                    ),
                ])
                direct_account_id, prompt_account_id = direct_account.id, prompt_account.id
            with Session() as session:
                direct_runtime = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=direct_account_id
                )
                prompt_runtime = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=prompt_account_id
                )
        finally:
            engine.dispose()

        self.assertEqual(direct_runtime.delivery_mode, "direct_message")
        self.assertEqual(direct_runtime.private_message_text, "Configured private reply")
        self.assertEqual(direct_runtime.followup_message_text, "Configured follow-up")
        self.assertEqual(prompt_runtime.delivery_mode, "prompt_request")
        self.assertEqual(prompt_runtime.private_message_text, "")
        self.assertEqual(prompt_runtime.followup_message_text, "")

    def test_runtime_defaults_legacy_prompt_opening_without_overwriting_saved_copy(self):
        """A missing legacy opening must get warm copy while saved copy remains authoritative."""

        helpers = self._helpers()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        try:
            with Session.begin() as session:
                owner = User(email="prompt-opening-owner@example.test")
                session.add(owner)
                session.flush()
                legacy = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458151",
                    provider="instagram",
                    connection_status="connected",
                )
                custom = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458152",
                    provider="instagram",
                    connection_status="connected",
                )
                session.add_all([legacy, custom])
                session.flush()
                session.add_all([
                    InstagramAutomationConfig(
                        instagram_account_id=legacy.id,
                        enabled=True,
                        trigger_keywords=["prompt"],
                        delivery_mode="prompt_request",
                    ),
                    InstagramAutomationConfig(
                        instagram_account_id=custom.id,
                        enabled=True,
                        trigger_keywords=["prompt"],
                        delivery_mode="prompt_request",
                        prompt_opening_message_text="My saved opening",
                    ),
                ])
                legacy_id, custom_id = legacy.id, custom.id
            with Session() as session:
                legacy_runtime = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=legacy_id
                )
                custom_runtime = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=custom_id
                )
        finally:
            engine.dispose()

        self.assertEqual(
            legacy_runtime.prompt_opening_message_text,
            "Hey 👋 Thanks for your interest! Your prompt is ready. Tap below and I’ll send it to you.",
        )
        self.assertEqual(custom_runtime.prompt_opening_message_text, "My saved opening")

    def test_runtime_snapshot_keeps_prompt_and_smart_reply_settings_account_scoped(self):
        """A runtime read must not mix tenant A's smart settings into tenant B."""

        helpers = self._helpers()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        try:
            with Session.begin() as session:
                owner = User(email="runtime-owner@example.test")
                session.add(owner)
                session.flush()
                account_a = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458131",
                    provider="instagram",
                    connection_status="connected",
                )
                account_b = InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458132",
                    provider="instagram",
                    connection_status="connected",
                )
                session.add_all([account_a, account_b])
                session.flush()
                session.add_all([
                    InstagramAutomationConfig(
                        instagram_account_id=account_a.id,
                        enabled=True,
                        trigger_keywords=["prompt"],
                        public_reply_enabled=False,
                        public_reply_text="",
                        smart_reply_enabled=True,
                        smart_reply_tone="professional",
                        smart_reply_max_per_hour=7,
                    ),
                    InstagramAutomationConfig(
                        instagram_account_id=account_b.id,
                        enabled=False,
                        trigger_keywords=["access"],
                        public_reply_enabled=False,
                        public_reply_text="",
                        smart_reply_enabled=False,
                        smart_reply_tone="concise",
                        smart_reply_max_per_hour=3,
                    ),
                ])
                account_a_id, account_b_id = account_a.id, account_b.id
            with Session() as session:
                runtime_a = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=account_a_id
                )
                runtime_b = helpers.oauth_automation_runtime_config(
                    session, instagram_account_id=account_b_id
                )
        finally:
            engine.dispose()

        self.assertTrue(runtime_a.smart_reply_enabled)
        self.assertEqual(runtime_a.smart_reply_tone, "professional")
        self.assertEqual(runtime_a.smart_reply_max_per_hour, 7)
        self.assertFalse(runtime_b.enabled)
        self.assertFalse(runtime_b.smart_reply_enabled)
        self.assertEqual(runtime_b.smart_reply_tone, "concise")
        self.assertEqual(runtime_b.smart_reply_max_per_hour, 3)

    def test_current_public_reply_default_matches_existing_oauth_behavior(self):
        """Changing the rollout copy must fail this behavior-preservation test."""

        helpers = self._helpers()
        self.assertEqual(
            helpers.current_default_public_reply_text(),
            "Thanks for commenting 🙌 I’ve sent you a DM.",
        )

    def test_initial_enabled_state_uses_account_prerequisites_not_global_environment(self):
        """Treating an environment kill switch as tenant config eligibility must fail."""

        helpers = self._helpers()
        now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
        account = InstagramAccount(
            user_id="11111111-1111-1111-1111-111111111111",
            instagram_user_id="17841438617458112",
            provider="instagram",
            connection_status="connected",
            token_expires_at=now + timedelta(days=1),
        )
        credential = InstagramCredential(
            token_ciphertext="encrypted-token",
            encryption_key_id="TEST_KEY",
            token_status="valid",
            scopes=[
                "instagram_business_manage_comments",
                "instagram_business_manage_messages",
            ],
            expires_at=now + timedelta(days=1),
        )
        self.assertTrue(helpers.initial_automation_enabled(account, credential, now=now))
        credential.scopes = ["instagram_business_manage_comments"]
        self.assertFalse(helpers.initial_automation_enabled(account, credential, now=now))


class TenantAutomationBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp_dir.name}/backfill.db")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _helpers(self):
        spec = importlib.util.find_spec("database.tenant_automation")
        self.assertIsNotNone(spec, "tenant automation helper module is required")
        return importlib.import_module("database.tenant_automation")

    def test_backfill_preserves_eligible_behavior_and_skips_legacy_or_existing_rows(self):
        """Overwriting tenant choices or enabling unusable/legacy accounts must fail."""

        helpers = self._helpers()
        with self.Session.begin() as session:
            owner = User(email="backfill-owner@example.test")
            session.add(owner)
            session.flush()
            eligible = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458113",
                provider="instagram",
                connection_status="connected",
                token_expires_at=self.now + timedelta(days=1),
            )
            unusable = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458114",
                provider="instagram",
                connection_status="connected",
            )
            legacy = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458115",
                provider="instagram",
                connection_status="environment_managed",
            )
            preserved = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458116",
                provider="instagram",
                connection_status="connected",
            )
            session.add_all([eligible, unusable, legacy, preserved])
            session.flush()
            session.add_all([
                InstagramCredential(
                    instagram_account_id=eligible.id,
                    token_ciphertext="eligible-ciphertext",
                    encryption_key_id="TEST_KEY",
                    token_status="valid",
                    scopes=[
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                    expires_at=self.now + timedelta(days=1),
                ),
                InstagramCredential(
                    instagram_account_id=unusable.id,
                    token_ciphertext="unusable-ciphertext",
                    encryption_key_id="TEST_KEY",
                    token_status="valid",
                    scopes=["instagram_business_manage_comments"],
                ),
                InstagramAutomationConfig(
                    instagram_account_id=preserved.id,
                    enabled=False,
                    trigger_keywords=["custom"],
                    public_reply_enabled=False,
                    public_reply_text="Custom tenant copy",
                ),
            ])
            ids = {
                "eligible": eligible.id,
                "unusable": unusable.id,
                "legacy": legacy.id,
                "preserved": preserved.id,
            }

        with self.Session.begin() as session:
            self.assertEqual(
                helpers.backfill_oauth_automation_configs(session, now=self.now), 2
            )
            self.assertEqual(
                helpers.backfill_oauth_automation_configs(session, now=self.now), 0
            )

        with self.Session() as session:
            configs = {
                config.instagram_account_id: config
                for config in session.scalars(select(InstagramAutomationConfig)).all()
            }
        self.assertTrue(configs[ids["eligible"]].enabled)
        self.assertEqual(configs[ids["eligible"]].delivery_mode, "direct_message")
        self.assertEqual(
            configs[ids["eligible"]].trigger_keywords,
            ["send", "info"],
        )
        self.assertTrue(configs[ids["eligible"]].public_reply_enabled)
        self.assertEqual(
            configs[ids["eligible"]].public_reply_text,
            "Thanks for commenting 🙌 I’ve sent you a DM.",
        )
        self.assertEqual(
            configs[ids["eligible"]].private_message_text,
            "Hey 👋 Thanks for your interest! I’ve got something for you.",
        )
        self.assertTrue(configs[ids["eligible"]].followup_enabled)
        self.assertEqual(configs[ids["eligible"]].followup_delay_minutes, 360)
        self.assertEqual(
            configs[ids["eligible"]].followup_message_text,
            "Just checking in 👋 Did you get a chance to see my message?",
        )
        self.assertFalse(configs[ids["unusable"]].enabled)
        self.assertNotIn(ids["legacy"], configs)
        self.assertEqual(configs[ids["preserved"]].trigger_keywords, ["custom"])
        self.assertFalse(configs[ids["preserved"]].public_reply_enabled)
        self.assertEqual(configs[ids["preserved"]].public_reply_text, "Custom tenant copy")


if __name__ == "__main__":
    unittest.main()
