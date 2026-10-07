#!/usr/bin/env bash
# One-time setup of the public online demo on a free PythonAnywhere account.
# Run it in a PythonAnywhere Bash console AFTER creating the web app (Web tab):
#   bash ~/clinic-records/scripts/pythonanywhere_setup.sh
set -euo pipefail
cd ~/clinic-records
HOST="${USER}.pythonanywhere.com"

echo "1/4 Installing packages (takes a few minutes)..."
python3.12 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "2/4 Writing settings (.env) with new random secrets..."
  SECRET=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(50))')
  DEMO_PW=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')
  cat > .env <<ENV
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=${SECRET}
DJANGO_ALLOWED_HOSTS=${HOST}
DJANGO_CSRF_TRUSTED_ORIGINS=https://${HOST}
SITE_URL=https://${HOST}
# PythonAnywhere's "Force HTTPS" switch (Web tab) does the http -> https redirect.
DJANGO_SECURE_SSL_REDIRECT=False
DEMO_MODE=True
DEMO_PASSWORD=${DEMO_PW}
ALLOW_CLINIC_SIGNUP=False
ENV
else
  echo "2/4 Keeping the existing .env"
fi

echo "3/4 Preparing styles and the demo clinic..."
.venv/bin/python manage.py collectstatic --noinput > /dev/null
bash scripts/reset_demo.sh

echo "4/4 Pointing the web app at this code..."
cat > "/var/www/${USER}_pythonanywhere_com_wsgi.py" <<'WSGI'
import os
import sys

path = os.path.expanduser("~/clinic-records")
if path not in sys.path:
    sys.path.insert(0, path)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
WSGI

echo
echo "Done. Now go to the Web tab and click the green Reload button."
echo "Your demo: https://${HOST}"
