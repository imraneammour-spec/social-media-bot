"""Conservative RSS drill for concurrent native web and probe-worker processes.

This is a staging feasibility measurement, not a Passenger or production load
benchmark. Run on Linux/FreeBSD; includes password hashing in the web process.
The actual account must also budget Passenger/provider processes and native builds.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(role, config, ready):
    from scripts.serv00 import load_config
    load_config(config)
    import webhook
    from worker import tick, worker_lock
    if role == "web":
        from werkzeug.security import generate_password_hash
        with webhook.app.test_client() as client:
            def work():
                for path in ("/", "/login", "/register", "/pricing", "/ready"):
                    if client.get(path).status_code != 200:
                        raise RuntimeError("Web drill request failed.")
                generate_password_hash("memory-drill-only-password")
            ready.touch()
            while True:
                work()
                time.sleep(0.1)
    else:
        with worker_lock(webhook.DATA_DIR):
            with webhook.app.test_client() as client:
                def work():
                    if client.get("/ready").status_code != 200:
                        raise RuntimeError("Worker readiness failed.")
                ready.touch()
                while True:
                    if not tick([("ready", work)], webhook.DATA_DIR, probe=True):
                        raise RuntimeError("Worker drill failed.")
                    time.sleep(0.1)


def measure(config, seconds=8):
    if os.name != "posix":
        raise RuntimeError("RSS drill needs Linux/FreeBSD ps; Windows is not a host acceptance test.")
    children = []
    peaks = [0, 0]
    with tempfile.TemporaryDirectory(prefix="postenza-memory-") as scratch:
        try:
            ready_files = [Path(scratch) / role for role in ("web", "worker")]
            for role, ready in zip(("web", "worker"), ready_files):
                children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--role", role, "--config", str(config), "--ready", str(ready)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT))
            deadline = time.monotonic() + 45
            while not all(p.exists() for p in ready_files):
                if any(p.poll() is not None for p in children) or time.monotonic() >= deadline:
                    raise RuntimeError("Memory drill processes did not become ready.")
                time.sleep(0.1)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for index, process in enumerate(children):
                    if process.poll() is not None:
                        raise RuntimeError("Memory drill process stopped.")
                    rss = subprocess.check_output(["ps", "-o", "rss=", "-p", str(process.pid)], text=True)
                    peaks[index] = max(peaks[index], int(rss.strip()) * 1024)
                time.sleep(0.1)
        finally:
            for process in children:
                if process.poll() is None:
                    process.terminate()
            for process in children:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    result = {"web_peak_mib": round(peaks[0] / 2**20, 1),
        "worker_peak_mib": round(peaks[1] / 2**20, 1),
        "sum_of_sampled_peaks_mib": round(sum(peaks) / 2**20, 1),
        "application_budget_mib": 384, "account_limit_mib": 512,
        "scope": "native staging requests plus hashing; excludes Passenger, uploads and live integrations"}
    print(json.dumps(result, sort_keys=True), flush=True)
    if sum(peaks) > 384 * 2**20:
        raise RuntimeError("Staging application exceeds the budget reserved below the account limit.")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--role", choices=["web", "worker"])
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--ci", action="store_true", help="Use only an explicit disposable TEST_DATABASE_URL.")
    args = parser.parse_args()
    try:
        if args.role:
            child(args.role, args.config, args.ready)
        elif args.ci:
            from sqlalchemy.engine import make_url
            url = os.environ["TEST_DATABASE_URL"]
            if not (make_url(url).database or "").endswith("_test"):
                raise ValueError("CI drill requires an explicitly disposable database.")
            with tempfile.TemporaryDirectory(prefix="postenza-serv00-") as scratch:
                config = Path(scratch) / "config.json"
                config.write_text(json.dumps({"DATABASE_URL": url,
                    "FLASK_SECRET_KEY": "ci-memory-drill-only-secret-at-least-32-chars",
                    "PUBLIC_BASE_URL": "https://preview.example.test",
                    "POSTENZA_DATA_DIR": str(Path(scratch) / "data")}), encoding="utf-8")
                config.chmod(0o600)
                measure(config)
        else:
            measure(args.config)
    except Exception as error:
        print("Memory drill failed: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
