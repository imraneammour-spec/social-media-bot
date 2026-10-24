"""Read-only host health checks; no notifications, restarts or provider actions."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

try:
    from scripts.host_backup import DockerCompose
except ModuleNotFoundError:
    from host_backup import DockerCompose


def health_problems(states, *, ready_ok, disk_percent, last_backup_at,
                    require_worker=False, require_backup=False, now=None):
    now = now or datetime.now(timezone.utc)
    problems = []
    if not ready_ok:
        problems.append("readiness_failed")
    for service in ("web", "postgres", "proxy", *(("worker",) if require_worker else ())):
        state, health = states.get(service, ("missing", ""))
        if state != "running" or (service != "proxy" and health != "healthy"):
            problems.append(service + "_unhealthy")
    if disk_percent >= 85:
        problems.append("disk_near_capacity")
    if require_backup and (last_backup_at is None or last_backup_at.tzinfo is None
            or not timedelta(minutes=-5) <= now - last_backup_at <= timedelta(hours=26)):
        problems.append("backup_missing_or_stale")
    return problems


def inspect_host(root):
    # Even a read-only Docker API call can activate docker.socket. Inspect systemd
    # first and report a stopped daemon without opening that socket.
    daemon = subprocess.run(["systemctl", "is-active", "--quiet", "docker.service"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if daemon.returncode:
        return ["docker_inactive"]
    raw = DockerCompose(root).run(["ps", "--all", "--format", "json"]).decode()
    rows = json.loads(raw) if raw.lstrip().startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
    states = {row["Service"]: (row.get("State"), row.get("Health", "")) for row in rows}
    ready_ok = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=5) as response:
            ready_ok = response.status == 200 and json.load(response).get("status") == "ready"
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage(root)
    last_backup_at = None
    try:
        receipt = json.loads((root / "backups" / "last-encrypted-backup.json").read_text())
        name = receipt["file"]
        if Path(name).name == name and (root / "backups" / "encrypted" / name).is_file():
            last_backup_at = datetime.fromisoformat(receipt["created_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return health_problems(states, ready_ok=ready_ok, disk_percent=100 * disk.used / disk.total,
        last_backup_at=last_backup_at,
        require_worker=os.getenv("POSTENZA_MONITOR_WORKER") == "true",
        require_backup=os.getenv("POSTENZA_MONITOR_BACKUPS") == "true")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        import fcntl
        # Brief quiesced backups/deployments are expected maintenance. A lock held
        # longer than 30 minutes must still produce a failing monitor result.
        with (args.root / "backups" / "maintenance.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                age = datetime.now(timezone.utc).timestamp() - os.fstat(lock.fileno()).st_mtime
                if age > 1800:
                    raise RuntimeError("Maintenance exceeded the monitoring grace period.")
                print("Postenza health: expected maintenance in progress.")
                sys.exit(0)
            problems = inspect_host(args.root)
    except Exception as error:
        print("Postenza health inspection failed:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
    print("Postenza health:", ", ".join(problems) if problems else "ok")
    sys.exit(1 if problems else 0)
