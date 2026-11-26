"""GitHub CI only: actual stopped-container volume capture and age round trip.

This reads only the job's disposable postenza-smoke container. It does not create
production recovery attestations, activate publishing, or access Railway.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.encrypted_backup import encrypt_file, decrypt_file
from scripts.host_backup import DockerCompose


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("This disposable-container drill is restricted to GitHub CI.")
    container = "postenza-smoke"
    backend = DockerCompose(Path.cwd())
    backend.web_container = lambda: container
    subprocess.run(["docker", "exec", container, "python", "-c",
        "from pathlib import Path; Path('/app/data/backup-smoke.txt').write_text('disposable-volume-backup-proof')"], check=True)
    with tempfile.TemporaryDirectory(prefix="postenza-volume-drill-") as directory:
        root = Path(directory)
        try:
            backend.native_run(["docker", "stop", "--time", "30", container])
            backend.archive_files(root / "app-data.tar")
            assert backend.web_image() == "postenza:check"
        finally:
            backend.native_run(["docker", "start", container])
        identity = root / "identity"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        recipient = subprocess.check_output(["age-keygen", "-y", str(identity)], text=True).strip()
        encrypt_file(root / "app-data.tar", root / "app-data.tar.age", recipient)
        decrypt_file(root / "app-data.tar.age", root / "restored.tar", identity)
        assert hashlib.file_digest((root / "app-data.tar").open("rb"), "sha256").digest() == hashlib.file_digest((root / "restored.tar").open("rb"), "sha256").digest()
        with tarfile.open(root / "restored.tar") as archive:
            member = next(m for m in archive if PurePosixPath(m.name).name == "backup-smoke.txt")
            assert archive.extractfile(member).read() == b"disposable-volume-backup-proof"
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18000/ready", timeout=3) as response:
                if response.status == 200:
                    print("Stopped Docker volume capture, age decrypt and restarted readiness verified on disposable CI data.")
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError("Disposable container did not become ready after restart.")


if __name__ == "__main__":
    main()
