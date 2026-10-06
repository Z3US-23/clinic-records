"""Shared helpers for the core app's tests."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.core.testing import ClinicTestCase
from apps.reminders.models import Reminder

KARACHI = ZoneInfo("Asia/Karachi")  # the test clinics' timezone

class CoreTestCase(ClinicTestCase):
    """ClinicTestCase plus "today" in the clinic's timezone and a reminder factory."""

    def setUp(self):
        self.today = timezone.localtime(timezone.now(), KARACHI).date()

    def at(self, day, hour, minute=0):
        """Aware datetime for `day` at hour:minute, clinic time."""
        return datetime.combine(day, time(hour, minute), tzinfo=KARACHI)

    def days_from_today(self, days):
        return self.today + timedelta(days=days)

    def make_reminder(self, patient, kind=Reminder.Kind.CUSTOM, due_date=None, status=Reminder.Status.PENDING, **kwargs):
        return Reminder.objects.create(
            clinic=patient.clinic,
            patient=patient,
            kind=kind,
            due_date=due_date or self.today,
            status=status,
            message=kwargs.pop("message", f"Hello {patient.first_name}"),
            **kwargs,
        )
