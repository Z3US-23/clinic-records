from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase

KARACHI = ZoneInfo("Asia/Karachi")  # the test clinics' timezone


class AppointmentTestCase(ClinicTestCase):
    """ClinicTestCase plus a patient and date helpers in the clinic's timezone."""

    def setUp(self):
        self.today = timezone.localtime(timezone.now(), KARACHI).date()
        self.tomorrow = self.today + timedelta(days=1)
        self.patient = self.make_patient(full_name="Ayesha Khan Baloch", phone="0300-1234567")

    def at(self, day, hour, minute=0, tz=KARACHI):
        """Aware datetime for `day` hour:minute in the clinic's timezone."""
        return datetime.combine(day, time(hour, minute), tzinfo=tz)

    def day_url(self, day=None, **params):
        query = {"date": day.isoformat()} if day else {}
        query.update(params)
        url = reverse("appointments:day")
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        return url

    def audit_entries(self, obj, action=None):
        entries = AuditLog.objects.filter(object_type=type(obj).__name__, object_id=str(obj.pk))
        if action:
            entries = entries.filter(action=action)
        return entries
