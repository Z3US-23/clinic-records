"""The daily `generate_reminders` command and the Django admin pages."""

from io import StringIO

from django.contrib import admin
from django.core.management import CommandError, call_command
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import make_appointment, make_clinic, make_patient
from apps.reminders.models import MessageTemplate, Reminder

from .base import ReminderTestCase


class GenerateRemindersCommandTests(ReminderTestCase):
    def setUp(self):
        super().setUp()
        make_appointment(self.patient, self.doctor, self.tomorrow_evening())
        self.closed = make_clinic("Closed Clinic", is_active=False)
        make_appointment(make_patient(self.closed), None, self.tomorrow_evening())

    def run_command(self, *args):
        out = StringIO()
        call_command("generate_reminders", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_runs_for_every_active_clinic(self):
        output = self.run_command()

        self.assertIn("Clinic Al-Noor Family Clinic: 1 new reminder", output)
        self.assertIn("Clinic Other Clinic: 0 new reminders", output)
        self.assertNotIn("Closed Clinic", output)
        self.assertEqual(Reminder.objects.filter(clinic=self.clinic).count(), 1)
        self.assertFalse(Reminder.objects.filter(clinic=self.closed).exists())

    def test_running_twice_creates_nothing_new(self):
        self.run_command()
        self.assertIn("Clinic Al-Noor Family Clinic: 0 new reminders", self.run_command())

    def test_one_clinic_by_slug(self):
        output = self.run_command("--clinic", self.clinic.slug)
        self.assertIn("Al-Noor Family Clinic", output)
        self.assertNotIn("Other Clinic", output)

    def test_unknown_slug_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command("--clinic", "no-such-clinic")


class AdminTests(ReminderTestCase):
    def test_models_are_registered(self):
        self.assertTrue(admin.site.is_registered(Reminder))
        self.assertTrue(admin.site.is_registered(MessageTemplate))

    def test_admin_pages_render(self):
        superuser = User.objects.create_superuser(email="admin@example.test", password="x", full_name="Platform Admin")
        reminder = self.make_reminder()
        template = MessageTemplate.objects.create(clinic=self.clinic, kind="follow_up", body="Hi {first_name}")
        self.client.force_login(superuser)
        for url in (
            reverse("admin:reminders_reminder_changelist"),
            reverse("admin:reminders_reminder_change", args=[reminder.pk]),
            reverse("admin:reminders_messagetemplate_changelist"),
            reverse("admin:reminders_messagetemplate_change", args=[template.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
