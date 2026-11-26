#!/bin/sh
# Run from the private checkout with an already installed native virtualenv.
# No service, crontab, database, DNS or website configuration is auto-activated.
set -eu
umask 077
: "${POSTENZA_PYTHON:?Set the absolute virtualenv Python path}"
: "${POSTENZA_CONFIG:?Set the absolute private JSON configuration path}"
: "${POSTENZA_PASSENGER_ROOT:?Set the existing staging public_python directory}"
"$POSTENZA_PYTHON" -m pip check
"$POSTENZA_PYTHON" scripts/serv00.py verify --config "$POSTENZA_CONFIG"
"$POSTENZA_PYTHON" scripts/serv00.py prepare --config "$POSTENZA_CONFIG" --passenger-root "$POSTENZA_PASSENGER_ROOT"
"$POSTENZA_PYTHON" scripts/serv00.py cron --config "$POSTENZA_CONFIG"
