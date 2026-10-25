"""Shared-host safety, bounded resources and real process-lock recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, MagicMock

import pytest
from sqlalchemy.pool import QueuePool


def test_serv00_postgres_pool_has_one_connection_and_no_overflow(monkeypatch):
    from database.runtime import create_database_engine
    monkeypatch.setenv("POSTENZA_HOST_PROFILE", "serv00")
    engine = create_database_engine("postgresql+psycopg://example:unused@localhost/test")
    try:
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 1
        assert engine.pool._max_overflow == 0
    finally:
        engine.dispose()


def test_worker_reuses_webhooks_engine(app, monkeypatch):
    from worker import build_tasks
    import webhook
    factory = webhook.app.extensions["phase3a_auth"].session_factory
    execute = Mock()
    monkeypatch.setattr("worker.execute_due_scheduled_content", execute)
    tasks = build_tasks(webhook)
    assert {name for name, _ in tasks} >= {"scheduled_content", "token_refresh"}
    dict(tasks)["scheduled_content"]()
    execute.assert_called_once_with(factory)


def test_failed_tick_does_not_report_healthy_heartbeat(tmp_path):
    from worker import tick
    following = Mock()
    assert tick([("broken", Mock(side_effect=RuntimeError("secret"))), ("next", following)], tmp_path) is False
    following.assert_called_once()
    assert not (tmp_path / "worker-heartbeat").exists()
    assert tick([("ok", following)], tmp_path) is True
    assert (tmp_path / "worker-heartbeat").exists()


def test_worker_lock_excludes_overlap_and_recovers_after_kill(tmp_path):
    # Actual OS lock across separate interpreters, including abrupt death.
    code = """
import sys, time
from pathlib import Path
from worker import worker_lock
with worker_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    time.sleep(60)
"""
    first = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "locked"
        second = subprocess.run([sys.executable, "-c", code.replace("time.sleep(60)", "pass"), str(tmp_path)], capture_output=True, text=True, timeout=10)
        assert second.returncode != 0
        assert "locked" not in second.stdout
    finally:
        first.kill()
        first.wait(timeout=10)
        first.stdout.close()
    third = subprocess.run([sys.executable, "-c", code.replace("time.sleep(60)", "pass"), str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert third.returncode == 0
    assert third.stdout.strip() == "locked"


def test_worker_refuses_soft_lock_fallback(tmp_path):
    from worker import worker_lock
    assert worker_lock(tmp_path).fallback_to_soft is False


@pytest.mark.skipif(os.name != "posix", reason="Requires Unix flock implementation.")
def test_unsupported_host_flock_fails_visibly(tmp_path, monkeypatch):
    import errno
    import filelock._unix
    from worker import main
    def unsupported(_):
        raise OSError(errno.ENOSYS, "flock unavailable")
    monkeypatch.setattr(filelock._unix, "_lock_fd_nonblocking", unsupported)
    monkeypatch.setenv("POSTENZA_DATA_DIR", str(tmp_path))
    with pytest.raises(OSError) as error:
        main(once=True, probe=True)
    assert error.value.errno == errno.ENOSYS


def configuration(tmp_path):
    return {
        "DATABASE_URL": "postgresql+psycopg://user:private@localhost/new_staging",
        "FLASK_SECRET_KEY": "test-only-secret-with-at-least-32-characters",
        "PUBLIC_BASE_URL": "https://preview.example.test",
        "POSTENZA_DATA_DIR": str(tmp_path / "shared" / "data"),
    }


def test_staging_profile_ignores_inherited_live_settings(tmp_path, monkeypatch):
    from scripts.serv00 import load_config
    config = tmp_path / "config.json"
    config.write_text(json.dumps(configuration(tmp_path)), encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.setenv("BILLING_MODE", "live")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "private")
    monkeypatch.setenv("SEARCH_INDEXING_ENABLED", "true")
    load_config(config)
    assert os.environ["BILLING_MODE"] == "disabled"
    assert os.environ["INSTAGRAM_ACCESS_TOKEN"] == ""
    assert os.environ["SEARCH_INDEXING_ENABLED"] == "false"
    assert os.environ["POSTENZA_EMBEDDED_SCHEDULER"] == "false"
    assert os.environ["POSTENZA_HOST_PROFILE"] == "serv00"


@pytest.mark.parametrize("key,value", [("BILLING_MODE", "live"), ("META_APP_SECRET", "secret"), ("PUBLIC_BASE_URL", "http://preview.example.test"), ("POSTENZA_DATA_DIR", "relative/data")])
def test_invalid_or_active_config_refused(tmp_path, key, value):
    from scripts.serv00 import load_config
    values = configuration(tmp_path)
    values[key] = value
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    config.chmod(0o600)
    with pytest.raises(ValueError):
        load_config(config)


def test_config_and_data_inside_release_refused(tmp_path):
    from scripts.serv00 import validate_config
    values = configuration(tmp_path)
    values["POSTENZA_DATA_DIR"] = str(Path.cwd() / "data")
    with pytest.raises(ValueError):
        validate_config(values, tmp_path / "config.json")


def test_bootstrap_refuses_overwrite_and_uses_absolute_paths(tmp_path):
    from scripts.serv00 import prepare
    config = tmp_path / "config.json"
    config.write_text(json.dumps(configuration(tmp_path)), encoding="utf-8")
    config.chmod(0o600)
    target = tmp_path / "website" / "public_python"
    target.mkdir(parents=True)
    prepare(config, target)
    bootstrap = target / "passenger_wsgi.py"
    assert repr(str(config)) in bootstrap.read_text(encoding="utf-8")
    assert "load_config" in bootstrap.read_text(encoding="utf-8")
    assert not (target / "public" / "config.json").exists()
    original = bootstrap.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(config, target)
    assert bootstrap.read_bytes() == original


@pytest.fixture(autouse=True)
def restore_environment():
    original = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(original)


def test_probe_runs_after_restart_without_changing_persistent_state(tmp_path):
    from scripts.serv00 import load_config
    values = configuration(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    config.chmod(0o600)
    load_config(config)
    data = Path(values["POSTENZA_DATA_DIR"])
    sentinel = data / "existing-content.json"
    sentinel.write_text('{"preserve": true}', encoding="utf-8")
    code = """
import sys
from pathlib import Path
from scripts.serv00 import load_config
from worker import worker_lock, tick
values = load_config(Path(sys.argv[1]))
data = Path(values['POSTENZA_DATA_DIR'])
with worker_lock(data):
    assert tick([('safe', lambda: None)], data, probe=True)
"""
    for _ in range(2):
        result = subprocess.run([sys.executable, "-c", code, str(config)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == '{"preserve": true}'
    assert (data / "worker-probe-heartbeat").exists()
    assert not (data / "worker-heartbeat").exists()


def test_probe_does_not_build_active_queue_tasks(app, monkeypatch, tmp_path):
    import worker
    import webhook
    monkeypatch.setattr(worker, "build_tasks", Mock(side_effect=AssertionError("publishing enabled")))
    monkeypatch.setattr(webhook.app, "test_client", MagicMock())
    webhook.app.test_client.return_value.__enter__.return_value.get.return_value.status_code = 200
    assert worker._serve(tmp_path, once=True, probe=True) == 0
    assert (tmp_path / "worker-probe-heartbeat").exists()


def test_serv00_rejects_oversized_image_before_decoding(tmp_path):
    from io import BytesIO
    from PIL import Image
    import warnings
    from scripts.serv00 import load_config
    values = configuration(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(values), encoding="utf-8")
    config.chmod(0o600)
    # A small compressed header can otherwise request hundreds of MB decoded.
    buffer = BytesIO()
    with Image.new("RGB", (2001, 2000)) as image:
        image.save(buffer, format="JPEG")
    buffer.seek(0)
    old_limit = Image.MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            load_config(config)
            with pytest.raises(Image.DecompressionBombWarning):
                Image.open(buffer)
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit
