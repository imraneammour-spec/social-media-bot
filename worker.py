"""Always-on worker for durable tenant queues and legacy persistent-file jobs."""
import logging
import os
import signal
import threading
import time

os.environ["POSTENZA_EMBEDDED_SCHEDULER"] = "false"

from database.config import database_url_from_environment
from database.content_publishing import execute_due_scheduled_content
from database.runtime import create_session_factory


LOGGER = logging.getLogger("postenza.worker")
STOP = threading.Event()


def run_once(tasks):
    """A failing integration must not prevent independent queues from running."""
    for name, task in tasks:
        try:
            task()
        except Exception as error:
            LOGGER.error("worker_task_failed task=%s error_type=%s", name, type(error).__name__)


def main():
    import webhook
    from database.token_refresh import refresh_expiring_tokens
    from billing import reconcile_subscriptions
    database_url = database_url_from_environment()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for the production worker.")
    factory = create_session_factory(database_url)
    tasks = [(name, getattr(webhook, name)) for name in (
        "process_daily_auto_story", "process_due_scheduled_stories", "process_due_followups",
        "process_due_oauth_webhook_work", "process_due_oauth_followups")]
    tasks.append(("scheduled_content", lambda: execute_due_scheduled_content(factory)))
    last_refresh = 0

    def token_refresh():
        nonlocal last_refresh
        if time.monotonic() - last_refresh > 3600:
            refresh_expiring_tokens(factory)
            reconcile_subscriptions(factory)
            last_refresh = time.monotonic()
    tasks.insert(0, ("token_refresh", token_refresh))
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    LOGGER.info("worker_started")
    while not STOP.is_set():
        run_once(tasks)
        (webhook.DATA_DIR / "worker-heartbeat").touch()
        STOP.wait(webhook.SCHEDULER_POLL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
