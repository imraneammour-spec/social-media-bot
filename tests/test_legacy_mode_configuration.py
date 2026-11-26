import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.models import Base, InstagramAccount, User


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **_kwargs):
        pass

    def start(self):
        return None


class LegacyModeConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'legacy-mode.db'}"

    def tearDown(self):
        module = sys.modules.pop("legacy_mode_configuration_webhook", None)
        if module is not None:
            module.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        self.temp_dir.cleanup()

    def _load_module(self, *, legacy_identity):
        environment = {
            "DATABASE_URL": self.database_url,
            "FLASK_SECRET_KEY": "legacy-mode-test-secret",
            "META_APP_SECRET": "legacy-mode-signing-secret",
            "GEMINI_API_KEY": "",
            "LEGACY_INSTAGRAM_USER_ID": legacy_identity,
        }
        spec = importlib.util.spec_from_file_location(
            "legacy_mode_configuration_webhook", MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with patch.dict(os.environ, environment, clear=False), patch("threading.Thread", _NoopThread):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)
        Base.metadata.create_all(module.app.extensions["phase3a_auth"].session_factory.kw["bind"])
        return module

    def test_unset_legacy_mode_starts_auth_without_a_reserved_identity(self):
        """A missing deployment setting must not retain the retired compiled account identity."""
        module = self._load_module(legacy_identity="")

        self.assertEqual(module.INSTAGRAM_USER_ID, "")
        self.assertEqual(
            module.app.extensions["phase3a_auth"].legacy_instagram_user_id,
            "",
        )

    def test_unset_legacy_mode_routes_a_connected_previously_reserved_identity_as_oauth(self):
        """With no reserved legacy identity, a connected account must reach only OAuth handling."""
        module = self._load_module(legacy_identity="")
        professional_id = "17841438617458035"
        session_factory = module.app.extensions["phase3a_auth"].session_factory
        with session_factory.begin() as session:
            owner = User(email="transitioned-oauth@example.test")
            session.add(owner)
            session.flush()
            session.add(InstagramAccount(
                user_id=owner.id,
                instagram_user_id=professional_id,
                provider="instagram",
                connection_status="connected",
            ))

        payload = {"entry": [{"id": professional_id, "changes": []}]}
        with patch.object(module, "process_oauth_webhook_entry") as oauth_handler, patch.object(
            module, "_process_legacy_webhook_entry"
        ) as legacy_handler:
            module.process_webhook_background(payload, "http://localhost")

        oauth_handler.assert_called_once()
        legacy_handler.assert_not_called()

    def test_configured_legacy_mode_routes_environment_managed_identity_as_legacy(self):
        """An explicit legacy deployment setting must preserve the existing legacy route."""
        legacy_identity = "17841438617458036"
        module = self._load_module(legacy_identity=legacy_identity)
        session_factory = module.app.extensions["phase3a_auth"].session_factory
        with session_factory.begin() as session:
            owner = User(email="legacy-route@example.test")
            session.add(owner)
            session.flush()
            session.add(InstagramAccount(
                user_id=owner.id,
                instagram_user_id=legacy_identity,
                provider="instagram",
                connection_status="environment_managed",
            ))

        payload = {"entry": [{"id": legacy_identity, "changes": []}]}
        with patch.object(module, "process_oauth_webhook_entry") as oauth_handler, patch.object(
            module, "_process_legacy_webhook_entry"
        ) as legacy_handler:
            module.process_webhook_background(payload, "http://localhost")

        legacy_handler.assert_called_once()
        oauth_handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
