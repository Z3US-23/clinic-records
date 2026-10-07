"""Opting out of reminders or archiving a patient takes their reminders off "To send" straight away."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.core.testing import ClinicTestCase
from apps.reminders.models import Reminder
from apps.reminders.services import generate_reminders

from .test_forms import patient_data

Kind, Status = Reminder.Kind, Reminder.Status


class PatientReminderRefreshTests(ClinicTestCase):
    def setUp(self):
        self.patient = self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        today = timezone.localdate()
        # An appointment tomorrow: its reminder is due today. Staff also wrote a message of their own.
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=1))
        generate_reminders(self.clinic)
        Reminder.objects.create(
            clinic=self.clinic, patient=self.patient, kind=Kind.CUSTOM, due_date=today, message="Hello Ayesha"
        )
        self.automatic = Reminder.objects.filter(patient=self.patient).exclude(kind=Kind.CUSTOM)
        self.assertEqual(list(self.automatic.values_list("kind", "status")), [(Kind.APPOINTMENT, Status.PENDING)])

    def statuses(self):
        return {r.kind: (r.status, r.skip_reason) for r in Reminder.objects.filter(patient=self.patient)}

    def test_opting_out_skips_the_automatic_reminders_at_once(self):
        self.login(self.receptionist)
        data = patient_data(full_name="Ayesha Khan", date_of_birth=self.patient.date_of_birth.isoformat())
        del data["reminders_opt_in"]  # the box is unticked
        response = self.client.post(reverse("patients:update", args=[self.patient.pk]), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.statuses(), {
            Kind.APPOINTMENT: (Status.SKIPPED, Reminder.SkipReason.SYSTEM),
            # A message staff wrote themselves is still theirs to send.
            Kind.CUSTOM: (Status.PENDING, ""),
        })
        self.assertEqual(response.context["nav_reminders_due"], 1)

        # Opting back in brings the automatic reminders back.
        data["reminders_opt_in"] = "on"
        self.client.post(reverse("patients:update", args=[self.patient.pk]), data)
        self.assertEqual(set(self.automatic.values_list("status", flat=True)), {Status.PENDING})

    def test_other_edits_leave_reminders_alone(self):
        self.login(self.receptionist)
        data = patient_data(full_name="Ayesha Khan", date_of_birth=self.patient.date_of_birth.isoformat(), city="Multan")
        self.client.post(reverse("patients:update", args=[self.patient.pk]), data)
        self.assertEqual(set(Reminder.objects.filter(patient=self.patient).values_list("status", flat=True)), {Status.PENDING})

    def test_archiving_skips_every_pending_reminder_and_restoring_brings_them_back(self):
        self.login(self.doctor)
        url = reverse("patients:archive", args=[self.patient.pk])
        response = self.client.post(url, follow=True)
        self.assertEqual(set(Reminder.objects.filter(patient=self.patient).values_list("status", flat=True)), {Status.SKIPPED})
        self.assertEqual(response.context["nav_reminders_due"], 0)

        self.client.post(url)
        self.assertEqual(set(self.automatic.values_list("status", flat=True)), {Status.PENDING})
