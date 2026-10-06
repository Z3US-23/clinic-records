"""Shared helpers for the reminders tests."""

from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase
from apps.reminders.models import Reminder, ReminderKind

KARACHI = ZoneInfo("Asia/Karachi")  # the test clinics' timezone (UTC+5)
KOLKATA = ZoneInfo("Asia/Kolkata")  # UTC+5:30
UTC = dt_timezone.utc

# Tuesday 6 October 2026, 11:00 am in Karachi.
FROZEN_NOW = datetime(2026, 10, 6, 6, 0, tzinfo=UTC)
FROZEN_TODAY = date(2026, 10, 6)


class ReminderTestCase(ClinicTestCase):
    """ClinicTestCase plus a patient and helpers for dates in the clinic's timezone."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate(timezone.now(), KARACHI)
        self.patient = self.make_patient(full_name="Ali Raza", phone="0300-1234567")

    def day(self, offset):
        """The date `offset` days from today (negative = in the past)."""
        return self.today + timedelta(days=offset)

    def at(self, day, hour=10, minute=0, tz=KARACHI):
        """Aware datetime for `day` hour:minute in the clinic's timezone."""
        return datetime.combine(day, time(hour, minute), tzinfo=tz)

    def tomorrow_evening(self):
        """11 pm tomorrow: always in the future and inside the default 1-day reminder window."""
        return self.at(self.day(1), 23, 0)

    def make_reminder(self, patient=None, kind=ReminderKind.CUSTOM, status=Reminder.Status.PENDING,
                      due_date=None, message="Hello Ali, please call us.", **fields):
        patient = patient or self.patient
        return Reminder.objects.create(
            clinic=patient.clinic,
            patient=patient,
            kind=kind,
            status=status,
            due_date=due_date or self.today,
            message=message,
            **fields,
        )

    def audit_entries(self, obj=None, action=None):
        entries = AuditLog.objects.all()
        if obj is not None:
            entries = entries.filter(object_type=type(obj).__name__, object_id=str(obj.pk))
        if action:
            entries = entries.filter(action=action)
        return entries


class FrozenClockTestCase(ReminderTestCase):
    """The clock is stopped at FROZEN_NOW (override `now` in a subclass for another moment)."""

    now = FROZEN_NOW

    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
