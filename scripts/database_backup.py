"""Consistent PostgreSQL dump plus fingerprints, with restore verification.

Passwords are passed through child-process environment variables, never argv.
Run on the source host with PostgreSQL client tools matching the server major.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def connection_settings(variable):
    value = os.environ.get(variable)
    if not value:
        raise ValueError(f"{variable} is required.")
    url = make_url(value)
    if not url.drivername.startswith("postgres"):
        raise ValueError("PostgreSQL is required.")
    settings = {"host": url.host, "port": url.port or 5432, "user": url.username,
                "password": url.password, "dbname": url.database}
    settings.update({key: value for key, value in url.query.items() if key in {"sslmode", "sslrootcert", "channel_binding"}})
    return {key: value for key, value in settings.items() if value is not None}


def environment(settings):
    result = os.environ.copy()
    for key, name in {"host":"PGHOST", "port":"PGPORT", "user":"PGUSER", "password":"PGPASSWORD", "dbname":"PGDATABASE", "sslmode":"PGSSLMODE", "sslrootcert":"PGSSLROOTCERT", "channel_binding":"PGCHANNELBINDING"}.items():
        if key in settings:
            result[name] = str(settings[key])
    return result


def fingerprints(connection):
    tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
    result = {}
    for (table,) in tables:
        digest = hashlib.sha256()
        count = 0
        # Sort by canonical JSONB text so every table, including composite keys, is deterministic.
        query = sql.SQL("SELECT to_jsonb(t)::text FROM public.{} t ORDER BY to_jsonb(t)::text COLLATE \"C\"").format(sql.Identifier(table))
        with connection.cursor(name="backup_fingerprints") as cursor:
            cursor.execute(query)
            for (row,) in cursor:
                digest.update(row.encode("utf-8") + b"\n")
                count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def run_client(arguments, settings):
    completed = subprocess.run(arguments, env=environment(settings), capture_output=True)
    if completed.returncode:
        # Provider errors may contain connection strings. Keep output class-only.
        raise RuntimeError(f"{arguments[0]} failed; private output withheld. Check client/server versions and connectivity.")


def backup(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    settings = connection_settings("DATABASE_URL")
    with psycopg.connect(**settings) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET TIME ZONE 'UTC'")
        snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
        manifest = fingerprints(connection)
        run_client(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--snapshot=" + snapshot,
                    "--file=" + str(directory / "database.dump")], settings)
    dump_hash = hashlib.sha256((directory / "database.dump").read_bytes()).hexdigest()
    (directory / "manifest.json").write_text(json.dumps({"dump_sha256": dump_hash, "tables": manifest}, indent=2), encoding="utf-8")
    for path in directory.iterdir():
        path.chmod(0o600)
    print("Backup complete. A restore verification is still required.")


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((directory / "database.dump").read_bytes()).hexdigest()
    if digest != manifest["dump_sha256"]:
        raise ValueError("Backup checksum mismatch.")
    settings = connection_settings("RESTORE_DATABASE_URL")
    with psycopg.connect(**settings) as connection:
        if connection.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' LIMIT 1").fetchone():
            raise ValueError("Refusing restore: the destination is not empty.")
    run_client(["pg_restore", "--dbname=" + str(settings["dbname"]), "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction", str(directory / "database.dump")], settings)
    with psycopg.connect(**settings) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET TIME ZONE 'UTC'")
        restored = fingerprints(connection)
    if restored != manifest["tables"]:
        raise ValueError("Restore fingerprint mismatch. Do not switch production traffic.")
    (directory / "restore-verification.json").write_text(json.dumps({"verified": True, "dump_sha256": digest,
        "tables": len(restored), "rows": sum(row["rows"] for row in restored.values())}, indent=2), encoding="utf-8")
    print("Restore verified: every public table row count and content fingerprint matches.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "verify"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        (backup if args.action == "backup" else verify)(args.directory)
    except Exception as error:
        print("Database operation failed:", type(error).__name__, file=sys.stderr)
        sys.exit(1)
