#!/usr/bin/env bash
set -euo pipefail
cd /srv/postenza
exec python3 scripts/host_backup.py /srv/postenza
