"""Host orchestration uses a fake Docker boundary and real age encryption.

All recovery attestations below are synthetic files inside pytest's disposable
directory. They are never installed in /srv/postenza or used for deployment.
"""
import hashlib
import json
from pathlib import Path
import tarfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import subprocess

import pytest

from recovery_tests.test_encrypted_backup import age_keys


class FakeCompose:
    def __init__(self, *, fail_dump=False):
        self.events = []
        self.fail_dump = fail_dump

    def running_apps(self):
        return ["web"]  # Worker is deliberately stopped on staging.

    def web_image(self):
        return "synthetic-test-image"

    def stop(self, services):
        self.events.append(("stop", services[:]))

    def start(self, services):
        if self.fail_dump:
            assert not (self.capture_dir / "database.dump").exists(), "Free failed scratch capture before restarting apps."
        self.events.append(("start", services[:]))

    def dump(self, target):
        self.capture_dir = target.parent
        self.events.append(("dump", []))
        if self.fail_dump:
            target.write_bytes(b"partial capture simulating a full scratch filesystem")
            raise RuntimeError("Simulated unavailable database")
        target.write_bytes(b"synthetic test database capture")

    def archive_files(self, target):
        self.events.append(("files", []))
        target.write_bytes(b"synthetic app archive")


@pytest.fixture
def host_root(tmp_path, monkeypatch):
    # Tiny synthetic captures need a small test cap, not 7 GiB on a shared runner.
    # Production's default remains 2 GiB/file + its conservative scratch reserve.
    monkeypatch.setenv("POSTENZA_BACKUP_MAX_FILE_BYTES", str(4 * 1024 ** 2))
    root = tmp_path / "disposable-host"
    recovered = root / "backups" / "production-recovery"
    recovered.mkdir(parents=True)
    dump = recovered / "database.dump"
    dump.write_bytes(b"synthetic unit fixture; never production data")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    (recovered / "manifest.json").write_text(json.dumps({"dump_sha256": digest, "tables": {"example": {}}}))
    (recovered / "restore-verification.json").write_text(json.dumps({"verified": True, "dump_sha256": digest, "tables": 1, "rows": 1}))
    (root / "backups" / "PRODUCTION_RECOVERY_APPROVED").write_text(digest)
    (root / ".env.production").write_text("TEST_ONLY=synthetic\n")
    return root


def test_host_backup_encrypts_all_captures_and_preserves_stopped_worker(host_root, age_keys):
    from scripts.host_backup import run_backup
    from scripts.encrypted_backup import decrypt_file
    age, identity, recipient = age_keys
    compose = FakeCompose()
    archive = run_backup(host_root, recipient, compose=compose, age_binary=age)
    assert compose.events == [("stop", ["web"]), ("dump", []), ("files", []), ("start", ["web"])]
    restored = host_root / "decrypted-test.tar"
    decrypt_file(archive, restored, identity, age_binary=age)
    with tarfile.open(restored) as bundle:
        manifest = json.load(bundle.extractfile("manifest.json"))
        assert manifest["web_image"] == "synthetic-test-image"
        assert set(manifest["files"]) == {"database.dump", "app-data.tar", "environment"}
        for name, expected in manifest["files"].items():
            data = bundle.extractfile(name).read()
            assert hashlib.sha256(data).hexdigest() == expected["sha256"]
            assert len(data) == expected["bytes"]
    assert (host_root / ".env.production").read_text() == "TEST_ONLY=synthetic\n"
    assert (host_root / "backups" / "production-recovery" / "database.dump").exists()
    assert not list((host_root / "backups").glob(".capture-*"))
    assert (host_root / "backups" / "last-encrypted-backup.json").exists()


def test_capture_failure_restarts_original_services_without_publishing_backup(host_root):
    from scripts.host_backup import run_backup
    compose = FakeCompose(fail_dump=True)
    with pytest.raises(RuntimeError):
        run_backup(host_root, "age1not-used", compose=compose)
    assert compose.events[-1] == ("start", ["web"])
    assert not list((host_root / "backups").rglob("*.age"))
    assert not (host_root / "backups" / "last-encrypted-backup.json").exists()


def test_encryption_failure_does_not_leave_apps_stopped(host_root, monkeypatch):
    from scripts import host_backup
    compose = FakeCompose()
    def fail(*args, **kwargs):
        raise RuntimeError("Simulated encryption failure")
    monkeypatch.setattr(host_backup, "encrypt_file", fail)
    with pytest.raises(RuntimeError):
        host_backup.run_backup(host_root, "age1not-used", compose=compose)
    assert compose.events[-1] == ("start", ["web"])
    assert not (host_root / "backups" / "last-encrypted-backup.json").exists()


def test_unverified_recovery_is_rejected_before_services_are_stopped(host_root):
    from scripts.host_backup import run_backup
    compose = FakeCompose()
    (host_root / "backups" / "production-recovery" / "database.dump").write_bytes(b"changed")
    with pytest.raises(ValueError):
        run_backup(host_root, "age1not-used", compose=compose)
    assert compose.events == []


def test_staging_health_does_not_require_worker_or_backup_activation():
    from scripts.host_monitor import health_problems
    states = {"web": ("running", "healthy"), "postgres": ("running", "healthy"),
              "proxy": ("running", ""), "worker": ("exited", "")}
    assert health_problems(states, ready_ok=True, disk_percent=30, last_backup_at=None) == []


def test_production_monitor_reports_stopped_worker_and_stale_backup():
    from scripts.host_monitor import health_problems
    now = datetime.now(timezone.utc)
    states = {"web": ("running", "healthy"), "postgres": ("running", "healthy"),
              "proxy": ("running", ""), "worker": ("exited", "")}
    problems = health_problems(states, ready_ok=True, disk_percent=30,
        last_backup_at=now - timedelta(hours=27), require_worker=True,
        require_backup=True, now=now)
    assert set(problems) == {"worker_unhealthy", "backup_missing_or_stale"}


def test_monitor_rejects_unready_database_unhealthy_containers_and_full_disk():
    from scripts.host_monitor import health_problems
    states = {"web": ("running", "unhealthy"), "postgres": ("running", "healthy")}
    problems = health_problems(states, ready_ok=False, disk_percent=91, last_backup_at=None)
    assert set(problems) == {"readiness_failed", "web_unhealthy", "proxy_unhealthy", "disk_near_capacity"}


def test_backup_reserves_capacity_for_captures_bundle_and_ciphertext(host_root, monkeypatch):
    from scripts import host_backup
    compose = FakeCompose()
    monkeypatch.setenv("POSTENZA_BACKUP_MAX_FILE_BYTES", str(2 * 1024 ** 3))
    monkeypatch.setattr(host_backup.shutil, "disk_usage", lambda root: SimpleNamespace(free=5 * 1024 ** 3))
    monkeypatch.setattr(host_backup, "encrypt_file", lambda source, output, *a, **k: output.write_bytes(b"synthetic"))
    with pytest.raises(ValueError):
        host_backup.run_backup(host_root, "age1not-used", compose=compose)
    assert compose.events == []


def test_file_capture_reuses_existing_container_without_pulling_another_image(host_root, monkeypatch):
    from scripts.host_backup import DockerCompose
    commands = []
    def docker(command, **kwargs):
        commands.append(command)
        if "ps" in command:
            return subprocess.CompletedProcess(command, 0, stdout=b"existing-web-id\n")
        return subprocess.CompletedProcess(command, 0, stdout=b"")
    monkeypatch.setattr(subprocess, "run", docker)
    DockerCompose(host_root).archive_files(host_root / "app-data.tar")
    assert any(command[:2] == ["docker", "cp"] and "existing-web-id:/app/data/." in command for command in commands)
    assert all("run" not in command and "pull" not in command and "build" not in command for command in commands)


def test_monitor_does_not_connect_to_socket_activated_stopped_docker(host_root, monkeypatch):
    from scripts import host_monitor
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 3))
    def forbidden_connection(*args):
        raise AssertionError("A Docker API call could socket-activate a stopped daemon.")
    monkeypatch.setattr(host_monitor, "DockerCompose", forbidden_connection)
    assert host_monitor.inspect_host(host_root) == ["docker_inactive"]
