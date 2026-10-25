"""Quiesced Docker host backups. Requires an independently verified recovery.

Only this program's new temporary captures are cleaned up. Original recovery
artifacts, source volumes and older encrypted backups are never removed.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile

try:
    from scripts.encrypted_backup import encrypt_file
except ModuleNotFoundError:  # Direct `python scripts/host_backup.py` invocation.
    from encrypted_backup import encrypt_file


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_recovery(root):
    recovered = root / "backups" / "production-recovery"
    receipt = json.loads((recovered / "restore-verification.json").read_text())
    manifest = json.loads((recovered / "manifest.json").read_text())
    approved = (root / "backups" / "PRODUCTION_RECOVERY_APPROVED").read_text().strip()
    if not (receipt.get("verified") is True and receipt.get("rows", 0) > 0
            and receipt.get("tables") == len(manifest["tables"])
            and approved == receipt["dump_sha256"] == manifest["dump_sha256"]
            == digest(recovered / "database.dump")):
        raise ValueError("A verified production recovery is required before host backups.")


class DockerCompose:
    def __init__(self, root):
        self.root = root
        self.command = ["docker", "compose", "--env-file", ".env.production", "-f", "deploy/compose.yml"]

    def native_run(self, command, *, stdout=subprocess.PIPE, stdin=None):
        result = subprocess.run(command, cwd=self.root, stdin=stdin,
                                stdout=stdout, stderr=subprocess.DEVNULL, check=False, timeout=600)
        if result.returncode:
            raise RuntimeError("Docker backup operation failed; private output withheld.")
        return result.stdout

    def run(self, arguments, **kwargs):
        return self.native_run(self.command + arguments, **kwargs)

    def web_container(self):
        identifier = self.run(["ps", "--all", "--quiet", "web"]).decode().strip()
        if not identifier or "\n" in identifier:
            raise ValueError("Exactly one existing web container is required.")
        return identifier

    def web_image(self):
        return json.loads(self.native_run(["docker", "inspect", "--format",
            "{{json .Config.Image}}", self.web_container()]))

    def running_apps(self):
        running = self.run(["ps", "--status", "running", "--services"]).decode().splitlines()
        return [name for name in ("web", "worker") if name in running]

    def stop(self, services):
        if services:
            self.run(["stop", "--timeout", "60", *services])

    def start(self, services):
        if services:
            # start only existing containers; never create/enable a stopped worker.
            self.run(["start", *services])

    def dump(self, target):
        with target.open("wb") as output:
            self.run(["exec", "-T", "postgres", "sh", "-c",
                      'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-owner --no-privileges'], stdout=output)
        with target.open("rb") as snapshot:
            self.run(["exec", "-T", "postgres", "pg_restore", "--list"], stdin=snapshot)

    def archive_files(self, target):
        with target.open("wb") as output:
            self.native_run(["docker", "cp", "--archive",
                self.web_container() + ":/app/data/.", "-"], stdout=output)


def run_backup(root, recipient, *, compose=None, age_binary=None):
    root = Path(root).resolve()
    if not recipient or not recipient.startswith("age1"):
        raise ValueError("Configure an age public recipient; keep its private identity off-host.")
    if not (root / ".env.production").is_file():
        raise ValueError("The reviewed deployment environment is required.")
    require_recovery(root)
    limit = int(os.environ.get("POSTENZA_BACKUP_MAX_FILE_BYTES", str(2 * 1024 ** 3)))
    reserve = 1024 ** 3
    if limit < 2 * 1024 ** 2 or (root / ".env.production").stat().st_size > 1024 ** 2:
        raise ValueError("Invalid backup size limit or oversized deployment configuration.")
    # Captures, bundle and ciphertext coexist. Keep a separate application reserve.
    if shutil.disk_usage(root).free < 3 * limit + reserve:
        raise ValueError("Insufficient backup scratch capacity plus application reserve.")
    backups = root / "backups"
    encrypted = backups / "encrypted"
    encrypted.mkdir(mode=0o700, exist_ok=True)
    compose = compose or DockerCompose(root)
    image = compose.web_image()
    running = compose.running_apps()
    with tempfile.TemporaryDirectory(prefix=".capture-", dir=backups) as directory:
        capture = Path(directory)
        try:
            compose.stop(running)
            compose.dump(capture / "database.dump")
            compose.archive_files(capture / "app-data.tar")
            shutil.copyfile(root / ".env.production", capture / "environment")
        except BaseException:
            # A full scratch filesystem must be released before apps restart.
            # These are only this invocation's new captures, never source data.
            for name in ("database.dump", "app-data.tar", "environment"):
                (capture / name).unlink(missing_ok=True)
            raise
        finally:
            # Also restore original running services after stop/capture failures.
            compose.start(running)
        now = datetime.now(timezone.utc)
        metadata = {"captured_at": now.isoformat(), "restore_verified": False, "web_image": image, "files": {}}
        names = ("database.dump", "app-data.tar", "environment")
        for name in names:
            path = capture / name
            metadata["files"][name] = {"bytes": path.stat().st_size, "sha256": digest(path)}
        captured_bytes = sum(item["bytes"] for item in metadata["files"].values())
        if captured_bytes + 1024 ** 2 > limit or shutil.disk_usage(root).free < 2 * limit + reserve:
            raise ValueError("Backup exceeds the bounded bundle/scratch capacity.")
        (capture / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        archive = capture / "bundle.tar"
        with tarfile.open(archive, "w") as bundle:
            for name in (*names, "manifest.json"):
                bundle.add(capture / name, arcname=name, recursive=False)
        if archive.stat().st_size + 1024 ** 2 > limit or shutil.disk_usage(root).free < limit + reserve:
            raise ValueError("Insufficient reserved capacity for encrypted output.")
        output = encrypted / (now.strftime("%Y%m%dT%H%M%S.%fZ") + ".tar.age")
        encrypt_file(archive, output, recipient, age_binary=age_binary)
        receipt = {"created_at": now.isoformat(), "file": output.name,
                   "sha256": digest(output), "restore_verified": False}
        temporary = capture / "last-encrypted-backup.json"
        temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, backups / "last-encrypted-backup.json")
        return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        raise InterruptedError("Backup interrupted; restore original service state.")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        # Shared with deployment; fcntl is deliberately Linux-only at the CLI boundary.
        import fcntl
        import resource
        limit = int(os.environ.get("POSTENZA_BACKUP_MAX_FILE_BYTES", str(2 * 1024 ** 3)))
        if limit <= 0:
            raise ValueError("A positive backup file-size limit is required.")
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        with (args.root / "backups" / "maintenance.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.utime(lock.name)
            run_backup(args.root, os.environ.get("POSTENZA_BACKUP_RECIPIENT", ""))
    except Exception as error:
        print("Host backup failed:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
    print("Encrypted host backup created. Separate restore and off-host verification are required.")
