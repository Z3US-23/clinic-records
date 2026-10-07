#!/usr/bin/env bash
# Fresh demo clinic dated around today. Run daily on PythonAnywhere (Tasks tab):
#   bash ~/clinic-records/scripts/reset_demo.sh
set -euo pipefail
cd ~/clinic-records
DEMO_PASSWORD=$(grep '^DEMO_PASSWORD=' .env | cut -d= -f2-)
.venv/bin/python manage.py migrate --noinput > /dev/null
.venv/bin/python manage.py seed_demo --reset --password "${DEMO_PASSWORD}" > /dev/null
echo "Demo clinic reset."
