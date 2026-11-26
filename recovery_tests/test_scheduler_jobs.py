"""Exercise the embedded loop without publishing or starting a background thread."""
from unittest.mock import Mock

import pytest


def test_embedded_loop_runs_every_queue_despite_one_failure(app, monkeypatch):
    import webhook
    names = ("process_oauth_maintenance", "process_daily_auto_story",
             "process_due_scheduled_stories", "process_due_followups",
             "process_due_oauth_webhook_work", "process_due_oauth_followups",
             "process_due_tenant_content")
    jobs = []
    for index, name in enumerate(names):
        job = Mock(side_effect=RuntimeError("private-provider-error") if index == 2 else None)
        job.__name__ = name
        monkeypatch.setattr(webhook, name, job)
        jobs.append(job)
    class StopLoop(BaseException):
        pass
    monkeypatch.setattr(webhook.time, "sleep", Mock(side_effect=StopLoop))
    legacy_publish = Mock()
    monkeypatch.setattr(webhook, "process_due_scheduled_posts", legacy_publish)
    with pytest.raises(StopLoop):
        webhook.dashboard_scheduler_loop()
    for job in jobs:
        job.assert_called_once()
    legacy_publish.assert_not_called()
