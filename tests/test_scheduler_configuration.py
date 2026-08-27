import ast
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename


MODULE_PATH = Path(__file__).resolve().parents[1] / "webhook.py"
SCHEDULER_CONFIG_NAMES = {
    "SCHEDULER_TIMEZONE",
    "SCHEDULER_POLL_SECONDS",
    "SCHEDULER_LOCKS_DIR",
}


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def load_scheduler_runtime(data_dir):
    """Load the real scheduler helpers without starting the app's daemon thread."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    requested_timezones = []

    def scheduler_zoneinfo(name):
        requested_timezones.append(name)
        if name != "Africa/Casablanca":
            raise ValueError(f"Unexpected scheduler timezone: {name}")
        return timezone(timedelta(hours=1), name)

    namespace = {
        "DATA_DIR": Path(data_dir),
        "os": os,
        "Path": Path,
        "ZoneInfo": scheduler_zoneinfo,
        "datetime": datetime,
        "timezone": timezone,
        "time": time,
        "secure_filename": secure_filename,
    }

    config_nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in SCHEDULER_CONFIG_NAMES
                for target in node.targets
            )
        )
        or (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "SCHEDULER_LOCKS_DIR"
            and node.value.func.attr == "mkdir"
        )
    ]
    helpers = [
        _function(tree, name)
        for name in (
            "parse_dashboard_schedule",
            "acquire_schedule_lock",
            "release_schedule_lock",
            "process_daily_auto_story",
            "process_due_oauth_followups",
            "dashboard_scheduler_loop",
        )
    ]
    exec(
        compile(ast.Module(body=[*config_nodes, *helpers], type_ignores=[]), str(MODULE_PATH), "exec"),
        namespace,
    )
    namespace["requested_timezones"] = requested_timezones
    return namespace


class SchedulerConfigurationTest(unittest.TestCase):
    def test_story_scheduling_and_locking_use_restored_scheduler_configuration(self):
        with tempfile.TemporaryDirectory() as data_dir:
            runtime = load_scheduler_runtime(data_dir)

            scheduled_utc, error = runtime["parse_dashboard_schedule"]("2035-01-01T20:00")

            self.assertEqual(error, "")
            self.assertEqual(
                scheduled_utc,
                datetime(2035, 1, 1, 19, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(runtime["requested_timezones"], ["Africa/Casablanca"])

            lock_path = runtime["acquire_schedule_lock"]("story-regression-test")

            self.assertIsNotNone(lock_path)
            self.assertTrue(lock_path.exists())
            self.assertEqual(lock_path.parent, Path(data_dir) / "scheduler_locks")
            self.assertIsNone(runtime["acquire_schedule_lock"]("story-regression-test"))
            runtime["release_schedule_lock"](lock_path)
            self.assertFalse(lock_path.exists())

    def test_daily_auto_story_uses_the_scheduler_timezone_before_and_after_due_time(self):
        with tempfile.TemporaryDirectory() as data_dir:
            runtime = load_scheduler_runtime(data_dir)
            queued = []
            runtime["AUTO_STORY_DAILY_TIME"] = "20:00"
            runtime["load_auto_story_settings"] = lambda: {
                "daily_enabled": True,
                "daily_time": "20:00",
            }
            runtime["queue_daily_archive_story"] = lambda now: queued.append(now)

            runtime["process_daily_auto_story"](
                datetime(2035, 1, 1, 18, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(queued, [])

            due_time = datetime(2035, 1, 1, 19, 0, tzinfo=timezone.utc)
            runtime["process_daily_auto_story"](due_time)
            self.assertEqual(queued, [due_time])

    def test_scheduler_loop_processes_story_and_followup_work_without_post_publishing(self):
        with tempfile.TemporaryDirectory() as data_dir:
            runtime = load_scheduler_runtime(data_dir)
            calls = []

            class StopSchedulerLoop(Exception):
                pass

            runtime["process_daily_auto_story"] = lambda: calls.append("daily")
            runtime["process_due_scheduled_stories"] = lambda: calls.append("stories")
            runtime["process_due_followups"] = lambda: calls.append("followups")
            runtime["process_due_oauth_followups"] = lambda: calls.append("oauth_followups")
            runtime["time"] = SimpleNamespace(
                sleep=lambda seconds: (_ for _ in ()).throw(StopSchedulerLoop(seconds))
            )

            with self.assertRaises(StopSchedulerLoop) as stopped:
                runtime["dashboard_scheduler_loop"]()

            self.assertEqual(stopped.exception.args, (15,))
            self.assertEqual(calls, ["daily", "stories", "followups", "oauth_followups"])


if __name__ == "__main__":
    unittest.main()
