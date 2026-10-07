"""The 'no clinic' page, switching clinics, and your own profile."""

from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.core.testing import make_clinic, make_user

from .base import AccountsTestCase


class NoClinicTests(AccountsTestCase):
    url = reverse("accounts:no_clinic")

    def test_requires_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_members_are_sent_to_dashboard(self):
        self.login(self.receptionist)
        self.assertRedirects(self.client.get(self.url), reverse("core:dashboard"), fetch_redirect_response=False)

    def test_deactivated_user_sees_explanation_and_sign_out(self):
        user = make_user(self.clinic, Membership.Role.DOCTOR, email="gone@example.test", is_active=False)
        self.login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not part of a clinic")
        self.assertContains(response, "open the join link")
        self.assertContains(response, reverse("accounts:logout"))

    def test_clinic_pages_send_deactivated_user_here(self):
        user = make_user(self.clinic, Membership.Role.OWNER, email="gone2@example.test", is_active=False)
        self.login(user)
        response = self.client.get(reverse("accounts:profile"))
        self.assertRedirects(response, self.url)


class SwitchClinicTests(AccountsTestCase):
    def setUp(self):
        # The doctor also works at the other clinic.
        Membership.objects.create(user=self.doctor, clinic=self.other_clinic, role=Membership.Role.DOCTOR)

    def url(self, clinic):
        return reverse("accounts:switch_clinic", args=[clinic.pk])

    def test_get_not_allowed(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(self.url(self.other_clinic)).status_code, 405)

    def test_switch_to_a_clinic_you_work_at(self):
        self.login(self.doctor)
        response = self.client.post(self.url(self.other_clinic))
        self.assertRedirects(response, reverse("core:dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.session["clinic_id"], self.other_clinic.pk)
        # The next page is now in the other clinic
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.context["current_clinic"], self.other_clinic)

    def test_switching_is_audited_in_both_clinics(self):
        self.login(self.doctor)
        self.client.post(self.url(self.other_clinic))

        left = AuditLog.objects.get(action=AuditLog.Action.LOGOUT, user=self.doctor)
        self.assertEqual(left.clinic, self.clinic)
        self.assertEqual(left.summary, "Switched to another clinic")
        arrived = AuditLog.objects.get(action=AuditLog.Action.LOGIN, user=self.doctor, clinic=self.other_clinic)
        self.assertEqual(arrived.summary, "Signed in (switched from another clinic)")
        # Neither entry names the other clinic.
        self.assertNotIn(self.other_clinic.name, left.summary)
        self.assertNotIn(self.clinic.name, arrived.summary)

    def test_switching_to_the_clinic_you_are_in_logs_nothing(self):
        self.login(self.doctor)
        self.client.post(self.url(self.clinic))
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.LOGOUT).exists())

    def test_refused_until_you_choose_your_own_password(self):
        User.objects.filter(pk=self.doctor.pk).update(must_change_password=True)
        self.login(self.doctor)
        response = self.client.post(self.url(self.other_clinic))
        self.assertRedirects(response, reverse("accounts:password_change"), fetch_redirect_response=False)
        self.assertNotEqual(self.client.session.get("clinic_id"), self.other_clinic.pk)

    def test_cannot_switch_to_someone_elses_clinic(self):
        self.login(self.receptionist)
        response = self.client.post(self.url(self.other_clinic))
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(self.client.session.get("clinic_id"), self.other_clinic.pk)

    def test_cannot_switch_to_clinic_where_access_is_switched_off(self):
        Membership.objects.filter(user=self.doctor, clinic=self.other_clinic).update(is_active=False)
        self.login(self.doctor)
        self.assertEqual(self.client.post(self.url(self.other_clinic)).status_code, 404)

    def test_cannot_switch_to_paused_clinic(self):
        paused = make_clinic("Paused Clinic", is_active=False)
        Membership.objects.create(user=self.doctor, clinic=paused, role=Membership.Role.DOCTOR)
        self.login(self.doctor)
        self.assertEqual(self.client.post(self.url(paused)).status_code, 404)

    def test_anonymous_redirected_to_login(self):
        response = self.client.post(self.url(self.clinic))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


class ProfileTests(AccountsTestCase):
    url = reverse("accounts:profile")

    def test_renders_for_every_role(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, user.email)
                self.assertContains(response, reverse("accounts:password_change"))

    def test_receptionist_has_no_prescription_fields(self):
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertIsNone(response.context["details_form"])
        self.assertNotContains(response, 'name="registration_number"')

    def test_doctor_updates_name_and_prescription_details(self):
        self.login(self.doctor)
        response = self.client.post(
            self.url,
            {
                "full_name": "Bilal Hussain Shah",
                "title": "Prof.",
                "qualifications": "MBBS, FCPS (Medicine)",
                "registration_number": "PMDC-12345",
            },
        )
        self.assertRedirects(response, self.url)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.full_name, "Bilal Hussain Shah")
        membership = Membership.objects.get(user=self.doctor, clinic=self.clinic)
        self.assertEqual(membership.title, "Prof.")
        self.assertEqual(membership.qualifications, "MBBS, FCPS (Medicine)")
        self.assertEqual(membership.registration_number, "PMDC-12345")
        self.assertTrue(
            AuditLog.objects.filter(action=AuditLog.Action.UPDATE, user=self.doctor, clinic=self.clinic).exists()
        )

    def test_receptionist_cannot_set_prescription_details(self):
        self.login(self.receptionist)
        response = self.client.post(
            self.url, {"full_name": "Hina Malik", "title": "Dr.", "registration_number": "FAKE-1"}
        )
        self.assertRedirects(response, self.url)
        membership = Membership.objects.get(user=self.receptionist)
        self.assertEqual(membership.title, "")
        self.assertEqual(membership.registration_number, "")

    def test_name_is_required(self):
        self.login(self.owner)
        response = self.client.post(self.url, {"full_name": "", "title": "Dr."})
        self.assertEqual(response.status_code, 200)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.full_name, "Sara Ahmed")
