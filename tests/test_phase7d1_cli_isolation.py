import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

from database.content_publishing import ContentPublishingSummary


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "webhook.py"
CLI_APP_MODULE = "database.cli_app"


class Phase7D1CliIsolationTest(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop(CLI_APP_MODULE, None)

    def _import_cli_app(self):
        sys.modules.pop(CLI_APP_MODULE, None)
        with patch(
            "threading.Thread",
            side_effect=AssertionError("safe CLI import must not create background threads"),
        ), patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("safe CLI import must not make provider requests"),
        ):
            return importlib.import_module(CLI_APP_MODULE)

    def test_safe_cli_import_registers_commands_without_background_side_effects(self):
        module = self._import_cli_app()

        self.assertIsInstance(module.app, Flask)
        self.assertIn("bootstrap-owner", module.app.cli.commands)
        self.assertIn("publish-scheduled-content", module.app.cli.commands)

    def test_safe_cli_publishing_command_invokes_only_the_mocked_service(self):
        module = self._import_cli_app()

        with patch("database.cli.database_url_from_environment", return_value="sqlite:///safe.db"), patch(
            "database.cli.create_session_factory", return_value="session-factory"
        ), patch(
            "database.cli.execute_due_scheduled_content",
            return_value=ContentPublishingSummary(claimed=0, published=0, failed=0, skipped=0),
        ) as execute:
            result = module.app.test_cli_runner().invoke(
                args=["publish-scheduled-content", "--limit", "1"]
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            result.output.strip(),
            "scheduled_content_execution claimed=0 published=0 failed=0 skipped=0",
        )
        execute.assert_called_once_with("session-factory", limit=1)

    def test_normal_web_runtime_starts_the_existing_dashboard_scheduler(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        module_name = "phase7d1_webhook_runtime"
        environment = {
            # The web runtime starts the embedded scheduler by default, but
            # recovery_tests/conftest.py sets POSTENZA_EMBEDDED_SCHEDULER=false
            # process-wide when the full suite runs, so pin it on here.
            "POSTENZA_EMBEDDED_SCHEDULER": "true",
            "DATABASE_URL": f"sqlite:///{Path(temporary_directory.name) / 'webhook.db'}",
            "FLASK_SECRET_KEY": "phase7d1-test-secret",
            "VERIFY_TOKEN": "phase7d1-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
        }
        spec = importlib.util.spec_from_file_location(module_name, WEBHOOK_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        thread = MagicMock()

        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread", return_value=thread
        ) as create_thread:
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)

        create_thread.assert_called_once_with(
            target=module.dashboard_scheduler_loop,
            name="dashboard-scheduler",
            daemon=True,
        )
        thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
