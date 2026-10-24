#!/usr/bin/env bash
set -euo pipefail
cd /srv/postenza
exec 9>backups/maintenance.lock
flock -x 9
touch backups/maintenance.lock
: "${POSTENZA_IMAGE:?Set an immutable validated image tag or digest}"
test -f .env.production
# A successful recovery drill is a hard deployment gate; never create a blank replacement.
python3 - <<'PY'
import hashlib, json
from pathlib import Path
source = Path("backups/production-recovery")
receipt = json.loads((source / "restore-verification.json").read_text())
manifest = json.loads((source / "manifest.json").read_text())
digest = hashlib.sha256((source / "database.dump").read_bytes()).hexdigest()
assert receipt["verified"] and receipt["dump_sha256"] == digest == manifest["dump_sha256"]
assert receipt["tables"] == len(manifest["tables"]) and receipt["rows"] > 0
assert Path("backups/PRODUCTION_RECOVERY_APPROVED").read_text().strip() == digest
PY
compose=(docker compose --env-file .env.production -f deploy/compose.yml)
"${compose[@]}" pull web worker
"${compose[@]}" stop worker
"${compose[@]}" run --rm --no-deps web python -m alembic upgrade head
"${compose[@]}" up -d web
for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8000/ready >/dev/null; then
    # Staging must not publish queued production content or send messages.
    # Start the worker separately after the cutover checks in RUNBOOK.md.
    "${compose[@]}" up -d proxy
    exit 0
  fi
  sleep 2
done
printf '%s\n' 'Readiness did not pass. Worker remains stopped; restore the previous image.' >&2
exit 1
