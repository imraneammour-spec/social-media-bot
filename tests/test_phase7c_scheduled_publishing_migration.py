import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "20260831_04_phase7c_scheduled_publishing.py"


class Phase7CScheduledPublishingMigrationTest(unittest.TestCase):
    def _module(self):
        self.assertTrue(MIGRATION_PATH.exists(), "Phase 7C migration must exist")
        spec = importlib.util.spec_from_file_location("phase7c_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_revision_is_forward_only_and_adds_publication_lifecycle_columns(self):
        module = self._module()
        self.assertEqual(module.revision, "20260831_04")
        self.assertEqual(module.down_revision, "20260831_03")

        with patch.object(module.op, "add_column") as add_column, patch.object(module.op, "create_index"):
            module.upgrade()

        self.assertEqual(
            [call.args[1].name for call in add_column.call_args_list],
            [
                "published_at",
                "provider_media_id",
                "publish_error_code",
                "provider_request_may_have_been_sent",
            ],
        )


if __name__ == "__main__":
    unittest.main()
