#!/usr/bin/env bash
# Starts the public online demo (see render.yaml).
#
# Each start builds a fresh "Demo Family Clinic" dated around today. The free server's disk is
# wiped whenever it restarts (for example after 15 idle minutes), which also clears whatever
# visitors changed.
set -euo pipefail

python manage.py migrate --noinput
python manage.py seed_demo --reset --password "${DEMO_PASSWORD}" > /dev/null
echo "Demo clinic ready."

# One process with a few threads: the demo's SQLite database and the in-memory sign-in
# lockout both need everything in one process.
exec gunicorn config.wsgi --bind "0.0.0.0:${PORT:-10000}" --workers 1 --threads 4 --timeout 60 --access-logfile -
