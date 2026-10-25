"""Native Serv00 staging commands. No Docker, systemd, DNS or publishing activation."""
import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED = {"DATABASE_URL", "FLASK_SECRET_KEY", "PUBLIC_BASE_URL", "POSTENZA_DATA_DIR"}
STAGING = {
    "APP_ENV": "production", "POSTENZA_HOST_PROFILE": "serv00",
    "POSTENZA_EMBEDDED_SCHEDULER": "false", "BILLING_MODE": "disabled",
    "BILLING_LIVE_APPROVED": "false", "SEARCH_INDEXING_ENABLED": "false",
    "TRUST_PROXY_HEADERS": "false", "ALLOW_UNSIGNED_WEBHOOKS": "false",
    "INSTAGRAM_ACCESS_TOKEN": "", "LEGACY_INSTAGRAM_USER_ID": "",
    "META_APP_SECRET": "", "META_APP_ID": "", "VERIFY_TOKEN": "",
    "INSTAGRAM_CLIENT_ID": "", "INSTAGRAM_CLIENT_SECRET": "",
    "INSTAGRAM_OAUTH_APP_ID": "", "INSTAGRAM_OAUTH_REDIRECT_URI": "",
    "GEMINI_API_KEY": "", "CLOUDINARY_CLOUD_NAME": "",
    "CLOUDINARY_API_KEY": "", "CLOUDINARY_API_SECRET": "",
    "LEMON_API_KEY": "", "LEMON_WEBHOOK_SECRET": "", "LEMON_STORE_ID": "",
}


def private_path(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Private paths must be absolute.")
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) or any(part in {"public", "public_html", "public_python"} for part in resolved.parts):
        raise ValueError("Keep private configuration and data outside the release and website.")
    return resolved


def validate_config(values, config_path):
    from sqlalchemy.engine import make_url
    private_path(config_path)
    if set(values) != REQUIRED or not all(isinstance(v, str) and v.strip() for v in values.values()):
        raise ValueError("Exactly the four documented staging settings are required.")
    private_path(values["POSTENZA_DATA_DIR"])
    url = make_url(values["DATABASE_URL"])
    if not url.drivername.startswith("postgresql") or not url.database:
        raise ValueError("A dedicated PostgreSQL staging database is required.")
    if len(values["FLASK_SECRET_KEY"]) < 32:
        raise ValueError("Use a fresh random Flask secret of at least 32 characters.")
    origin = urlsplit(values["PUBLIC_BASE_URL"])
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
        raise ValueError("Use an HTTPS staging origin without a path or credentials.")


def load_config(config_path):
    config_path = Path(config_path)
    if os.name != "nt" and stat.S_IMODE(config_path.stat().st_mode) & 0o077:
        raise ValueError("Private config must have mode 600 or stricter.")
    values = json.loads(config_path.read_text(encoding="utf-8"))
    validate_config(values, config_path)
    os.environ.update(values)
    os.environ.update(STAGING)  # Apply before Flask/cron imports; inherited live flags cannot win.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    # Bound decoded uploads before allocation, including compressed JPEG bombs.
    # The shared-host staging profile does not run full-size image generation.
    import warnings
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 4_000_000
    warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
    data = Path(values["POSTENZA_DATA_DIR"])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt" and stat.S_IMODE(data.stat().st_mode) & 0o077:
        raise ValueError("Persistent data must have mode 700 or stricter.")
    os.chdir(ROOT)
    return values


def prepare(config_path, target):
    values = load_config(config_path)
    target = Path(target).resolve()
    if not target.is_dir() or target.name != "public_python":
        raise ValueError("Select an existing Passenger public_python directory.")
    if Path(config_path).resolve().is_relative_to(target) or Path(values["POSTENZA_DATA_DIR"]).resolve().is_relative_to(target):
        raise ValueError("Private paths cannot be inside the website.")
    contents = (
        "# Generated staging entrypoint; private config stays outside this website.\n"
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.serv00 import load_config\n"
        f"load_config({str(Path(config_path).resolve())!r})\n"
        "from wsgi import app as application\n"
    )
    with (target / "passenger_wsgi.py").open("x", encoding="utf-8") as output:
        output.write(contents)


def migrate_fresh(config_path, confirmation):
    """Explicit fresh initialization only. Never upgrade/replace an existing target."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from database.config import database_url_from_environment
    from database.runtime import create_database_engine
    from alembic import command
    from alembic.config import Config
    values = load_config(config_path)
    url = database_url_from_environment()
    if confirmation != make_url(url).database:
        raise ValueError("Confirm the exact new staging database name.")
    engine = create_database_engine(url)
    try:
        with engine.connect() as connection:
            count = connection.scalar(text("""SELECT count(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
                AND c.relkind IN ('r','p','v','m','S','f')"""))
            if count:
                raise ValueError("Fresh initialization refuses a nonempty database. No data changed.")
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        command.upgrade(config, "head")
    finally:
        engine.dispose()
    return {"database": "fresh-staging", "railway_recovery": False, "migrations": "head"}


def verify(config_path):
    load_config(config_path)
    from webhook import app, DATA_DIR
    checks = {}
    with app.test_client() as client:
        for path in ("/health", "/ready", "/", "/login", "/register", "/pricing", "/robots.txt", "/sitemap.xml"):
            response = client.get(path, base_url=os.environ["PUBLIC_BASE_URL"])
            checks[path] = response.status_code == 200 and "noindex" in response.headers.get("X-Robots-Tag", "")
        checks["robots_blocked"] = "Disallow: /" in client.get("/robots.txt").text
        checks["csrf_enforced"] = client.post("/login", data={}).status_code == 400
    from billing import mode
    checks["billing_disabled"] = mode() == "disabled"
    # Exclusive temporary probe: never overwrite application files.
    import tempfile
    with tempfile.TemporaryFile(dir=DATA_DIR) as stream:
        stream.write(b"persistent-storage-probe")
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        checks["storage_writable"] = stream.read() == b"persistent-storage-probe"
    if not all(checks.values()):
        raise RuntimeError("Staging verification failed: " + ",".join(k for k, v in checks.items() if not v))
    return {"checks": checks, "scope": "in-process staging; public TLS/Passenger/cron still require remote checks"}


def cron_line(config_path):
    # Cron treats percent and newline specially even inside shell quotes.
    parts = [sys.executable, str(ROOT / "scripts" / "serv00.py"), "tick", "--config", str(Path(config_path).resolve())]
    if any(any(c in part for c in "%\n\r") for part in parts):
        raise ValueError("Cron paths must not contain percent or newline characters.")
    return "* * * * * " + " ".join(shlex.quote(p) for p in parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify", "tick", "cron", "migrate-fresh"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--passenger-root", type=Path)
    parser.add_argument("--confirm-fresh-database")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args.config, args.passenger_root)
            print("Staging Passenger entrypoint prepared; no website/cron/DNS activated.")
        elif args.action == "verify":
            print(json.dumps(verify(args.config), sort_keys=True))
        elif args.action == "migrate-fresh":
            print(json.dumps(migrate_fresh(args.config, args.confirm_fresh_database)))
        elif args.action == "cron":
            load_config(args.config)
            print(cron_line(args.config))
        else:
            load_config(args.config)
            from worker import main as worker_main
            return worker_main(once=True, probe=True)
    except Exception as error:
        # Database URLs, provider payloads and private configuration never enter logs.
        print("Serv00 staging command failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
