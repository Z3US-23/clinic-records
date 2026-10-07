"""Extra checks run by `manage.py check --deploy` before going live."""

from urllib.parse import urlsplit

from django.conf import settings
from django.core import checks
from django.http.request import validate_host


@checks.register(checks.Tags.security, deploy=True)
def check_site_url_host_is_allowed(app_configs, **kwargs):
    """Patients' confirmation links use SITE_URL. Django answers "Bad Request" for a host that is
    not in ALLOWED_HOSTS, so the links would not open."""
    host = urlsplit(settings.SITE_URL).hostname or ""
    if validate_host(host, settings.ALLOWED_HOSTS):
        return []
    return [
        checks.Warning(
            f"SITE_URL ({settings.SITE_URL}) is on a host that is not in DJANGO_ALLOWED_HOSTS, "
            "so the confirmation links in patients' WhatsApp reminders will not open.",
            hint=f"Add {host or 'the SITE_URL host'} to DJANGO_ALLOWED_HOSTS, or fix SITE_URL.",
            id="core.W001",
        )
    ]
