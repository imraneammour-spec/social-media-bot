"""BYOK Gemini keys (database/gemini_keys.py + webhook api_key threading).

Covers encrypted per-user key storage, resolution for an account's owner,
live validation outcomes, and the webhook helper using a caller-supplied
key instead of the shared client.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.credentials import CredentialKeyring
from database.gemini_keys import (
    GeminiKeyError,
    clear_user_gemini_key,
    load_user_gemini_key,
    mark_user_gemini_key_invalid,
    mark_user_gemini_key_validated,
    resolve_account_gemini_key,
    store_user_gemini_key,
    user_gemini_key_status,
    validate_gemini_api_key,
)
from database.models import Base, InstagramAccount, User, UserGeminiKey

import gemini_client

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


def _test_keyring():
    return CredentialKeyring(
        active_key_id="K1", _keys={"K1": Fernet(Fernet.generate_key())}
    )


def _other_keyring():
    return CredentialKeyring(
        active_key_id="K2", _keys={"K2": Fernet(Fernet.generate_key())}
    )


class GeminiKeysTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'k.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.keyring = _test_keyring()
        with self.Session.begin() as session:
            owner = User(email="owner@example.test", display_name="Owner")
            session.add(owner)
            session.flush()
            self.owner_id = owner.id
            session.add(
                InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="17841438617458289",
                    username="imran.prompts",
                    connection_status="environment_managed",
                )
            )
            self.account_id = session.scalars(
                __import__("sqlalchemy").select(InstagramAccount)
            ).first().id

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_store_load_round_trip_and_hint(self):
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            row = store_user_gemini_key(session, user, "AIzaTestKey1234", self.keyring)
            self.assertEqual(row.key_hint, "1234")
            self.assertEqual(row.key_status, "active")
            self.assertNotIn("AIzaTestKey1234", row.key_ciphertext or "")
        with self.Session() as session:
            self.assertEqual(
                load_user_gemini_key(session, self.owner_id, self.keyring),
                "AIzaTestKey1234",
            )
            status = user_gemini_key_status(session, self.owner_id)
        self.assertEqual(
            status,
            {
                "configured": True,
                "hint": "1234",
                "status": "active",
                "last_validated_at": None,
            },
        )

    def test_store_rejects_empty_key(self):
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            with self.assertRaises(GeminiKeyError):
                store_user_gemini_key(session, user, "   ", self.keyring)

    def test_load_returns_none_without_key_or_with_wrong_keyring(self):
        with self.Session() as session:
            self.assertIsNone(
                load_user_gemini_key(session, self.owner_id, self.keyring)
            )
            self.assertEqual(
                user_gemini_key_status(session, self.owner_id)["configured"],
                False,
            )
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            store_user_gemini_key(session, user, "AIzaTestKey1234", self.keyring)
        with self.Session() as session:
            self.assertIsNone(
                load_user_gemini_key(session, self.owner_id, _other_keyring())
            )

    def test_mark_invalid_blocks_load_and_validated_restores(self):
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            store_user_gemini_key(session, user, "AIzaTestKey1234", self.keyring)
        with self.Session.begin() as session:
            mark_user_gemini_key_invalid(session, self.owner_id, "api_key_invalid")
        with self.Session() as session:
            self.assertIsNone(
                load_user_gemini_key(session, self.owner_id, self.keyring)
            )
            self.assertEqual(
                user_gemini_key_status(session, self.owner_id)["status"],
                "invalid",
            )
        with self.Session.begin() as session:
            mark_user_gemini_key_validated(session, self.owner_id)
        with self.Session() as session:
            self.assertEqual(
                load_user_gemini_key(session, self.owner_id, self.keyring),
                "AIzaTestKey1234",
            )
            self.assertIsNotNone(
                user_gemini_key_status(session, self.owner_id)[
                    "last_validated_at"
                ]
            )

    def test_clear_removes_key(self):
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            store_user_gemini_key(session, user, "AIzaTestKey1234", self.keyring)
        with self.Session.begin() as session:
            self.assertTrue(clear_user_gemini_key(session, self.owner_id))
            self.assertFalse(clear_user_gemini_key(session, self.owner_id))
        with self.Session() as session:
            self.assertEqual(
                user_gemini_key_status(session, self.owner_id)["configured"],
                False,
            )

    def test_resolve_account_gemini_key(self):
        with self.Session.begin() as session:
            user = session.get(User, self.owner_id)
            store_user_gemini_key(session, user, "AIzaTestKey1234", self.keyring)
        self.assertEqual(
            resolve_account_gemini_key(
                self.Session, self.account_id, keyring=self.keyring
            ),
            "AIzaTestKey1234",
        )
        self.assertIsNone(
            resolve_account_gemini_key(
                self.Session, "00000000-0000-0000-0000-000000000000",
                keyring=self.keyring,
            )
        )
        self.assertIsNone(resolve_account_gemini_key(None, self.account_id))

    def test_validate_with_fake_client(self):
        class FakeModels:
            def __init__(self, behavior):
                self.behavior = behavior

            def list(self, page_size=1):
                if self.behavior == "ok":
                    return iter([MagicMock()])
                if self.behavior == "auth":
                    error = Exception("401 UNAUTHENTICATED: API key not valid")
                    error.code = 401
                    raise error
                raise ConnectionError("network down")

        def factory_for(behavior):
            def factory(api_key):
                client = MagicMock()
                client.models = FakeModels(behavior)
                client.used_key = api_key
                return client

            return factory

        self.assertEqual(
            validate_gemini_api_key("AIzaX", client_factory=factory_for("ok")),
            "valid",
        )
        self.assertEqual(
            validate_gemini_api_key("AIzaX", client_factory=factory_for("auth")),
            "invalid",
        )
        self.assertEqual(
            validate_gemini_api_key("AIzaX", client_factory=factory_for("down")),
            "unknown",
        )
        self.assertEqual(validate_gemini_api_key("   "), "invalid")


class WebhookByokThreadingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir()
        environment = {
            "DATABASE_URL": f"sqlite:///{Path(self.temp_dir.name) / 'w.db'}",
            "FLASK_SECRET_KEY": "byok-test-secret",
            "VERIFY_TOKEN": "byok-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            # Empty on purpose: constructing a real genai.Client at import
            # reads the ambient proxy env vars and fails in this sandbox.
            "GEMINI_API_KEY": "",
            "POSTENZA_DATA_DIR": str(self.data_dir),
            "POSTENZA_EMBEDDED_SCHEDULER": "false",
        }
        self.module_name = "phase_big_webhook_byok"
        self.module = self._load_webhook_module(environment, self.module_name)

    def _load_webhook_module(self, environment, module_name):
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)
        return module

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def test_gemini_generate_content_uses_caller_key(self):
        fake_response = MagicMock()
        fake_response.text = "hello"
        fake_client = MagicMock()
        fake_client.models.generate_content.return_value = fake_response
        created = {}

        def fake_client_factory(api_key):
            created["api_key"] = api_key
            return fake_client

        with patch.object(gemini_client.genai, "Client", side_effect=fake_client_factory):
            result = self.module._gemini_generate_content(
                "hi", api_key="AIzaUserKey"
            )
        self.assertEqual(result.text, "hello")
        self.assertEqual(created["api_key"], "AIzaUserKey")
        fake_client.models.generate_content.assert_called_once()

    def test_oauth_smart_reply_returns_generic_reply_without_gemini(self):
        # Postenza v2: no Gemini smart replies. The reply is the ready
        # generic text with a user key, with the shared client, or with
        # neither — and no Gemini client is ever built for it.
        from comment_pipeline import DEFAULT_GENERIC_REPLY

        self.module.GEMINI_CLIENT = None
        fake_client = MagicMock()
        with patch.object(gemini_client.genai, "Client", return_value=fake_client):
            reply = self.module.generate_oauth_smart_reply(
                "Beautiful work!", api_key="AIzaUserKey"
            )
        self.assertEqual(reply, DEFAULT_GENERIC_REPLY)
        fake_client.models.generate_content.assert_not_called()
        # And with neither a client nor a key it still replies.
        self.assertEqual(
            self.module.generate_oauth_smart_reply("Hi"), DEFAULT_GENERIC_REPLY
        )


if __name__ == "__main__":
    unittest.main()
