"""Quick status changes (POST appointments:set_status), the transition table and the admin."""

from unittest import mock

from django.urls import reverse

from apps.accounts.models import User
from apps.appointments import status as appt_status
from apps.appointments.models import Appointment
from apps.core.models import AuditLog

from .base import AppointmentTestCase

Status = Appointment.Status
REFRESH = "apps.reminders.services.refresh_for_appointment"


class SetStatusTests(AppointmentTestCase):
    def setUp(self):
        super().setUp()
        self.appointment = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        self.url = reverse("appointments:set_status", args=[self.appointment.pk])

    def test_get_is_not_allowed(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    @mock.patch(REFRESH)
    def test_every_role_can_change_status(self, refresh):
        for user, new_status in [
            (self.receptionist, Status.ARRIVED), (self.doctor, Status.COMPLETED), (self.owner, Status.ARRIVED),
        ]:
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.post(self.url, {"status": new_status})
                self.assertRedirects(response, self.day_url(self.tomorrow), fetch_redirect_response=False)
                self.appointment.refresh_from_db()
                self.assertEqual(self.appointment.status, new_status)
        self.assertEqual(refresh.call_count, 3)

    @mock.patch(REFRESH)
    def test_marks_no_show_with_audit(self, refresh):
        self.login(self.receptionist)
        response = self.client.post(self.url, {"status": Status.NO_SHOW}, follow=True)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.NO_SHOW)
        refresh.assert_called_once_with(self.appointment)
        log = self.audit_entries(self.appointment, AuditLog.Action.UPDATE).get()
        self.assertEqual(log.summary, f"Marked {self.patient.mrn} as Did not come")
        self.assertEqual(log.user, self.receptionist)
        self.assertContains(response, "Did not come")

    @mock.patch(REFRESH)
    def test_disallowed_change_is_refused(self, refresh):
        Appointment.objects.filter(pk=self.appointment.pk).update(status=Status.CANCELLED)
        self.login(self.receptionist)
        response = self.client.post(self.url, {"status": Status.ARRIVED}, follow=True)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.CANCELLED)
        self.assertContains(response, "can&#x27;t be changed")
        refresh.assert_not_called()
        self.assertFalse(self.audit_entries(self.appointment).exists())

    def test_unknown_status_is_a_bad_request(self):
        self.login(self.receptionist)
        for value in ("", "deleted", "ARRIVED"):
            with self.subTest(value=value):
                self.assertEqual(self.client.post(self.url, {"status": value}).status_code, 400)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.SCHEDULED)

    def test_same_status_changes_nothing(self):
        self.login(self.receptionist)
        self.client.post(self.url, {"status": Status.SCHEDULED})
        self.assertFalse(self.audit_entries(self.appointment).exists())

    def test_other_clinic_gets_404(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.post(self.url, {"status": Status.CANCELLED}).status_code, 404)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.SCHEDULED)

    def test_requires_login(self):
        response = self.client.post(self.url, {"status": Status.CANCELLED})
        self.assertEqual(response.status_code, 302)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.SCHEDULED)

    def test_next_must_be_on_this_site(self):
        self.login(self.receptionist)
        safe = self.day_url(self.tomorrow, doctor=self.doctor.pk)
        response = self.client.post(self.url, {"status": Status.ARRIVED, "next": safe})
        self.assertRedirects(response, safe, fetch_redirect_response=False)

        for unsafe in ("https://evil.example/", "//evil.example/", "javascript:alert(1)"):
            with self.subTest(next=unsafe):
                response = self.client.post(self.url, {"status": Status.SCHEDULED, "next": unsafe})
                self.assertRedirects(response, self.day_url(self.tomorrow), fetch_redirect_response=False)


class TransitionTableTests(AppointmentTestCase):
    def test_every_quick_button_is_an_allowed_change(self):
        for current, actions in appt_status.QUICK_ACTIONS.items():
            for action in actions:
                with self.subTest(current=current, to=action.status):
                    self.assertTrue(appt_status.can_change(current, action.status))

    def test_cancelled_is_final(self):
        for new_status in Status.values:
            self.assertFalse(appt_status.can_change(Status.CANCELLED, new_status))

    def test_every_status_has_a_label_and_a_rule(self):
        for value in Status.values:
            self.assertIn(value, appt_status.STATUS_LABELS)
            self.assertIn(value, appt_status.ALLOWED_TRANSITIONS)
            self.assertIn(value, appt_status.QUICK_ACTIONS)


class AdminTests(AppointmentTestCase):
    def test_admin_pages(self):
        admin_user = User.objects.create_superuser(email="root@example.test", password="x", full_name="Root")
        appointment = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        self.client.force_login(admin_user)
        changelist = self.client.get(reverse("admin:appointments_appointment_changelist"))
        self.assertEqual(changelist.status_code, 200)
        change = self.client.get(reverse("admin:appointments_appointment_change", args=[appointment.pk]))
        self.assertEqual(change.status_code, 200)
        self.assertContains(change, appointment.confirm_token)
        self.assertNotContains(change, 'name="confirm_token"')  # read-only
