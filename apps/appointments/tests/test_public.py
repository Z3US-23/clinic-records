"""The patient's public confirmation page: /c/<token>/ (no login)."""

from datetime import timedelta
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.core.models import AuditLog

from .base import AppointmentTestCase

Status = Appointment.Status
REFRESH = "apps.reminders.services.refresh_for_appointment"


class PublicConfirmTests(AppointmentTestCase):
    def setUp(self):
        super().setUp()
        self.clinic.address = "12 Mall Road"
        self.clinic.city = "Lahore"
        self.clinic.phone = "042-35761234"
        self.clinic.save()
        # A week from today at 3:30 pm clinic time, so the test does not depend on when it runs.
        self.when = self.at(self.today + timedelta(days=7), 15, 30)
        self.appointment = self.make_appointment(self.patient, when=self.when, reason="Diabetes review")
        self.url = reverse("public:confirm", args=[self.appointment.confirm_token])

    def refresh(self):
        self.appointment.refresh_from_db()
        return self.appointment

    # --- what the page shows ---------------------------------------------------

    def test_shows_only_what_the_patient_needs(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appointments/public_confirm.html")
        self.assertContains(response, "Al-Noor Family Clinic")
        self.assertContains(response, "Hello Ayesha,")
        self.assertContains(response, self.when.strftime("%A"))
        self.assertContains(response, f"{self.when.day} {self.when:%B %Y}")
        self.assertContains(response, "3:30 p.m.")
        self.assertContains(response, "with Dr. Bilal Hussain")
        self.assertContains(response, "12 Mall Road, Lahore")
        self.assertContains(response, 'href="tel:+924235761234"')
        self.assertContains(response, "Yes, I will come")
        self.assertContains(response, "I need another time")

        # Nothing else about the patient.
        self.assertNotContains(response, "Ayesha Khan Baloch")
        self.assertNotContains(response, "Khan")
        self.assertNotContains(response, self.patient.mrn)
        self.assertNotContains(response, "Diabetes review")
        self.assertNotContains(response, "1234567")  # the patient's own phone number

    def test_never_cached(self):
        response = self.client.get(self.url)
        self.assertIn("no-store", response["Cache-Control"])

    def test_time_is_shown_in_the_clinic_timezone(self):
        self.clinic.timezone = "Asia/Kolkata"
        self.clinic.save()
        # 15:30 in Karachi is 16:00 in India.
        response = self.client.get(self.url)
        self.assertContains(response, "4:00 p.m.")
        self.assertNotContains(response, "3:30 p.m.")

    def test_clinic_timezone_wins_over_a_signed_in_users_clinic(self):
        self.clinic.timezone = "Asia/Kolkata"
        self.clinic.save()
        self.login(self.other_owner)  # their clinic is on Pakistan time
        response = self.client.get(self.url)
        self.assertContains(response, "4:00 p.m.")

    def test_any_doctor_hides_the_doctor_line(self):
        Appointment.objects.filter(pk=self.appointment.pk).update(doctor=None)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "with Dr.")

    def test_unknown_token_is_404(self):
        bad_url = reverse("public:confirm", args=["not-a-real-token"])
        self.assertEqual(self.client.get(bad_url).status_code, 404)
        self.assertEqual(self.client.post(bad_url, {"answer": "confirm"}).status_code, 404)

    def test_inactive_clinic_is_404(self):
        self.clinic.is_active = False
        self.clinic.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_patient_name_is_escaped(self):
        self.patient.full_name = "<script>alert(1)</script> Khan"
        self.patient.save()
        response = self.client.get(self.url)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_forms_carry_a_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get(self.url)
        self.assertContains(response, "csrfmiddlewaretoken", count=2)
        # A POST without the token is refused.
        self.assertEqual(client.post(self.url, {"answer": "confirm"}).status_code, 403)
        self.assertEqual(self.refresh().status, Status.SCHEDULED)

    def test_other_methods_not_allowed(self):
        self.assertEqual(self.client.put(self.url).status_code, 405)
        self.assertEqual(self.client.delete(self.url).status_code, 405)

    # --- answering ---------------------------------------------------------------

    @mock.patch(REFRESH)
    def test_patient_confirms(self, refresh):
        response = self.client.post(self.url, {"answer": "confirm"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

        appointment = self.refresh()
        self.assertEqual(appointment.status, Status.CONFIRMED)
        self.assertIsNotNone(appointment.patient_responded_at)
        refresh.assert_called_once_with(appointment)

        log = self.audit_entries(appointment, AuditLog.Action.UPDATE).get()
        self.assertEqual(log.summary, "Patient confirmed via link")
        self.assertEqual(log.clinic, self.clinic)
        self.assertIsNone(log.user)

        page = self.client.get(self.url)
        self.assertContains(page, "Thank you!")
        self.assertNotContains(page, "Yes, I will come")  # already answered
        self.assertContains(page, "I need another time")  # can still change their mind

    @mock.patch(REFRESH)
    def test_patient_asks_for_another_time(self, refresh):
        response = self.client.post(self.url, {"answer": "reschedule", "note": "  Friday evening please  "})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

        appointment = self.refresh()
        self.assertEqual(appointment.status, Status.RESCHEDULE_REQUESTED)
        self.assertEqual(appointment.patient_note, "Friday evening please")
        self.assertIsNotNone(appointment.patient_responded_at)
        refresh.assert_called_once_with(appointment)
        log = self.audit_entries(appointment, AuditLog.Action.UPDATE).get()
        self.assertEqual(log.summary, "Patient asked for another time")
        self.assertEqual(log.clinic, self.clinic)

        page = self.client.get(self.url)
        self.assertContains(page, "We have told the clinic you need another time")
        self.assertContains(page, "Yes, I will come")  # can still confirm after all

    def test_note_is_optional(self):
        self.client.post(self.url, {"answer": "reschedule"})
        appointment = self.refresh()
        self.assertEqual(appointment.status, Status.RESCHEDULE_REQUESTED)
        self.assertEqual(appointment.patient_note, "")

    def test_note_longer_than_255_characters_is_refused(self):
        response = self.client.post(self.url, {"answer": "reschedule", "note": "x" * 256})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 255 characters")
        self.assertEqual(self.refresh().status, Status.SCHEDULED)

    def test_unknown_answer_changes_nothing(self):
        for answer in ("", "cancelled", "arrived"):
            with self.subTest(answer=answer):
                response = self.client.post(self.url, {"answer": answer})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.refresh().status, Status.SCHEDULED)
        self.assertFalse(self.audit_entries(self.appointment).exists())

    def test_confirming_clears_an_earlier_note(self):
        self.client.post(self.url, {"answer": "reschedule", "note": "Can't come Monday"})
        self.client.post(self.url, {"answer": "confirm"})
        appointment = self.refresh()
        self.assertEqual(appointment.status, Status.CONFIRMED)
        self.assertEqual(appointment.patient_note, "")

    # --- appointments that can no longer be changed -----------------------------

    def assert_locked(self, expected_status):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This appointment can no longer be changed online. Please call the clinic.")
        self.assertNotContains(response, 'name="answer"')
        self.assertNotContains(response, "Yes, I will come")

        before = self.refresh().patient_responded_at
        for answer in ("confirm", "reschedule"):
            response = self.client.post(self.url, {"answer": answer})
            self.assertEqual(response.status_code, 400)
            self.assertContains(response, "Please call the clinic", status_code=400)
        appointment = self.refresh()
        self.assertEqual(appointment.status, expected_status)
        self.assertEqual(appointment.patient_responded_at, before)
        self.assertFalse(self.audit_entries(appointment).exists())

    def test_past_appointment_is_locked(self):
        Appointment.objects.filter(pk=self.appointment.pk).update(scheduled_at=timezone.now() - timedelta(minutes=1))
        self.assert_locked(Status.SCHEDULED)

    def test_cancelled_seen_waiting_and_no_show_are_locked(self):
        for status in (Status.CANCELLED, Status.COMPLETED, Status.ARRIVED, Status.NO_SHOW):
            with self.subTest(status=status):
                Appointment.objects.filter(pk=self.appointment.pk).update(status=status)
                self.assert_locked(status)
