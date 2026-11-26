"""Always-on worker for durable tenant queues and legacy persistent-file jobs."""
import argparse
import logging
import os
from pathlib import Path
import signal
import threading
import time

os.environ["POSTENZA_EMBEDDED_SCHEDULER"] = "false"

from database.content_publishing import execute_due_scheduled_content
from filelock import FileLock, Timeout


LOGGER = logging.getLogger("postenza.worker")
STOP = threading.Event()


def run_once(tasks):
    """A failing integration must not prevent independent queues from running."""
    healthy = True
    for name, task in tasks:
        try:
            task()
        except Exception as error:
            healthy = False
            LOGGER.error("worker_task_failed task=%s error_type=%s", name, type(error).__name__)
    return healthy


def worker_lock(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    # OS-backed lock: an interrupted process cannot leave a stale PID gate.
    return FileLock(str(data_dir / "worker.lock"), timeout=0, fallback_to_soft=False)


def tick(tasks, data_dir, *, probe=False):
    healthy = run_once(tasks)
    if healthy:
        (data_dir / ("worker-probe-heartbeat" if probe else "worker-heartbeat")).touch()
    return healthy


def build_tasks(webhook):
    from database.token_refresh import refresh_expiring_tokens
    from billing import reconcile_subscriptions
    factory = webhook.app.extensions["phase3a_auth"].session_factory
    if factory is None:
        raise RuntimeError("DATABASE_URL is required for the production worker.")
    tasks = [(name, getattr(webhook, name)) for name in (
        "process_daily_auto_story", "process_due_scheduled_stories", "process_due_followups",
        "process_due_oauth_webhook_work", "process_due_oauth_followups")]
    tasks.append(("scheduled_content", lambda: execute_due_scheduled_content(factory)))
    maintenance = webhook.DATA_DIR / "worker-maintenance"

    def token_refresh():
        if not maintenance.exists() or time.time() - maintenance.stat().st_mtime >= 3600:
            refresh_expiring_tokens(factory)
            reconcile_subscriptions(factory)
            maintenance.touch()
    tasks.insert(0, ("token_refresh", token_refresh))
    return tasks


def main(*, once=False, probe=False):
    # Acquire before heavy application imports so overlapping cron invocations
    # don't each allocate another web-sized Python interpreter.
    base = Path(__file__).resolve().parent
    fallback = Path("/app/data") if Path("/app/data").exists() else base / "data"
    data_dir = Path(os.getenv("POSTENZA_DATA_DIR", str(fallback)))
    try:
        lock = worker_lock(data_dir)
        lock.acquire()
    except Timeout:
        LOGGER.info("worker_already_running")
        return 0
    try:
        return _serve(data_dir, once=once, probe=probe)
    finally:
        lock.release()


def _serve(data_dir, *, once, probe):
    import webhook
    if probe:
        def readiness():
            with webhook.app.test_client() as client:
                if client.get("/ready").status_code != 200:
                    raise RuntimeError("Worker probe database is not ready.")
        tasks = [("readiness", readiness)]
    else:
        tasks = build_tasks(webhook)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    LOGGER.info("worker_started probe=%s once=%s", probe, once)
    while not STOP.is_set():
        healthy = tick(tasks, data_dir, probe=probe)
        if once:
            return 0 if healthy else 1
        STOP.wait(webhook.SCHEDULER_POLL_SECONDS)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One serial queue cycle for cron.")
    parser.add_argument("--probe", action="store_true", help="Readiness/storage only; never execute integration jobs.")
    args = parser.parse_args()
    raise SystemExit(main(once=args.once, probe=args.probe))
