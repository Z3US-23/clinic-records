"""The public online demo: one-click sign-in, the banner, and the switched-off actions."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Clinic, Membership
from apps.core.management.commands.seed_demo import DEMO_SLUG, STAFF
from apps.core.testing import ClinicTestCase, make_clinic, make_user
from apps.patients.models import Patient

DEMO_EMAIL = {person["key"]: person["email"] for person in STAFF}
BANNER = "Online demo with made-up patients"


def demo_login_url(role):
    return reverse("core:demo_login", args=[role])


class DemoOffTests(ClinicTestCase):
    """DEMO_MODE is off by default: nothing about the demo exists."""

    def test_one_click_sign_in_does_not_exist(self):
        make_user(make_clinic("Demo"), Membership.Role.DOCTOR, email=DEMO_EMAIL["doctor"])
        response = self.client.post(demo_login_url("doctor"))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_banner_and_no_buttons(self):
        page = self.client.get(reverse("accounts:login")).content.decode()
        self.assertNotIn(BANNER, page)
        self.assertNotIn("Try the demo", page)

    def test_real_numbers_allowed_outside_the_demo(self):
        self.login(self.receptionist)
        response = self.client.post(
            reverse("patients:create"), {"full_name": "Real Number", "sex": "F", "phone": "0300-7654321"}
        )
        self.assertEqual(response.status_code, 302)

    def test_guard_lets_everything_through(self):
        self.login(self.owner)
        self.client.post(reverse("accounts:clinic_settings"), {"name": "Renamed"})
        # The settings form may reject this partial post, but the demo guard must not be what stops it.
        self.assertNotIn("switched off in the online demo", self.client.get(reverse("core:dashboard")).content.decode())


@override_settings(DEMO_MODE=True)
class DemoSignInTests(ClinicTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.demo_clinic = make_clinic("Demo Family Clinic", slug=DEMO_SLUG)
        cls.demo_doctor = make_user(cls.demo_clinic, Membership.Role.DOCTOR, email=DEMO_EMAIL["doctor"], full_name="Bilal Hussain")
        cls.demo_reception = make_user(
            cls.demo_clinic, Membership.Role.RECEPTIONIST, email=DEMO_EMAIL["reception"], full_name="Hina Malik"
        )
        cls.demo_owner = make_user(cls.demo_clinic, Membership.Role.OWNER, email=DEMO_EMAIL["owner"], full_name="Sara Ahmed")

    def test_sign_in_page_offers_the_three_roles(self):
        page = self.client.get(reverse("accounts:login")).content.decode()
        self.assertIn(BANNER, page)
        self.assertIn("Try the demo", page)
        for role in ("doctor", "receptionist", "owner"):
            self.assertIn(demo_login_url(role), page)

    def test_one_click_sign_in_for_each_role(self):
        for role, user in [("doctor", self.demo_doctor), ("receptionist", self.demo_reception), ("owner", self.demo_owner)]:
            with self.subTest(role=role):
                self.client.logout()
                response = self.client.post(demo_login_url(role))
                self.assertRedirects(response, reverse("core:dashboard"), fetch_redirect_response=False)
                self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
                self.assertEqual(self.client.session["clinic_id"], self.demo_clinic.pk)
                dashboard = self.client.get(reverse("core:dashboard"))
                self.assertEqual(dashboard.status_code, 200)
                self.assertContains(dashboard, BANNER)

    def test_only_post(self):
        self.assertEqual(self.client.get(demo_login_url("doctor")).status_code, 405)

    def test_unknown_role(self):
        self.assertEqual(self.client.post(demo_login_url("admin")).status_code, 404)

    def test_cannot_sign_in_as_anyone_else(self):
        # Only the three demo emails: never a real clinic's staff, whatever the URL says.
        for path in ("/demo/doctor@example.test/", "/demo/owner@example.test/", "/demo/Doctor/", "/demo/%2E%2E/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path).status_code, 404)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_never_into_a_real_clinic(self):
        # A demo account that also belongs to a real clinic is refused outright.
        Membership.objects.create(
            user=self.demo_doctor, clinic=self.clinic, role=Membership.Role.DOCTOR, is_active=True,
            accepted_at=self.demo_clinic.created_at,
        )
        response = self.client.post(demo_login_url("doctor"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_demo_clinic_not_ready_yet(self):
        Membership.objects.filter(user=self.demo_doctor).update(is_active=False)
        response = self.client.post(demo_login_url("doctor"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(DEMO_MODE=True)
class DemoGuardTests(ClinicTestCase):
    """Changes that would spoil the demo for the next visitor are refused; normal work is not."""

    def assertRefused(self, response):
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(response["Location"])
        self.assertContains(follow, "switched off in the online demo")

    def test_clinic_settings_are_read_only(self):
        self.login(self.owner)
        response = self.client.post(reverse("accounts:clinic_settings"), {"name": "Spoilt"}, HTTP_REFERER="http://testserver/accounts/settings/")
        self.assertRefused(response)
        self.assertEqual(Clinic.objects.get(pk=self.clinic.pk).name, self.clinic.name)
        self.assertEqual(self.client.get(reverse("accounts:clinic_settings")).status_code, 200)  # still viewable

    def test_staff_cannot_be_changed(self):
        self.login(self.owner)
        membership = Membership.objects.get(user=self.doctor, clinic=self.clinic)
        for name, args in [
            ("accounts:staff_edit", [membership.pk]),
            ("accounts:staff_set_password", [membership.pk]),
            ("accounts:staff_invite", [membership.pk]),
            ("accounts:staff_add", []),
        ]:
            with self.subTest(name=name):
                self.assertRefused(self.client.post(reverse(name, args=args), {"role": "receptionist", "is_active": ""}))
        membership.refresh_from_db()
        self.assertEqual(membership.role, Membership.Role.DOCTOR)
        self.assertTrue(membership.is_active)

    def test_own_password_and_profile_cannot_be_changed(self):
        self.login(self.doctor)
        self.assertRefused(self.client.post(reverse("accounts:password_change"), {"old_password": "x"}))
        self.assertRefused(self.client.post(reverse("accounts:profile"), {"full_name": "Someone Else"}))
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.full_name, "Bilal Hussain")

    def test_file_uploads_are_refused(self):
        self.login(self.doctor)
        patient = self.make_patient()
        pdf = SimpleUploadedFile("report.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
        response = self.client.post(
            reverse("clinical:lab_create", args=[patient.pk]),
            {"test_name": "CBC", "result_date": "2026-10-01", "file": pdf},
        )
        self.assertRefused(response)
        self.assertFalse(patient.lab_results.exists())

    def test_large_uploads_are_refused_before_reading(self):
        self.login(self.owner)
        big = SimpleUploadedFile("patients.csv", b"x" * (300 * 1024), content_type="text/csv")
        self.assertRefused(self.client.post(reverse("patients:import"), {"file": big}))

    def test_everyday_work_still_works(self):
        self.login(self.receptionist)
        response = self.client.post(
            reverse("patients:create"),
            {"full_name": "Demo Visitor Patient", "sex": "F", "phone": "0390-1234567", "reminders_opt_in": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Patient.objects.filter(full_name="Demo Visitor Patient").exists())

    def test_big_text_is_refused_before_reading(self):
        self.login(self.receptionist)
        response = self.client.post(
            reverse("patients:create"),
            {"full_name": "Huge Notes", "sex": "F", "phone": "0390-1234567", "notes": "x" * (300 * 1024)},
        )
        self.assertRefused(response)
        self.assertFalse(Patient.objects.filter(full_name="Huge Notes").exists())

    def test_only_made_up_phone_numbers(self):
        # Another visitor's "Send on WhatsApp" must never reach a real person.
        self.login(self.receptionist)
        for field, number in [("phone", "0300-7654321"), ("whatsapp_phone", "+92 333 1234567")]:
            with self.subTest(field=field):
                data = {"full_name": f"Real {field}", "sex": "M", "phone": "0390-1112223", field: number}
                response = self.client.post(reverse("patients:create"), data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "made-up number starting 0390-")
                self.assertFalse(Patient.objects.filter(full_name=f"Real {field}").exists())
        patient = self.make_patient(phone="0390-1112223")
        response = self.client.post(
            reverse("patients:update", args=[patient.pk]),
            {"full_name": patient.full_name, "sex": "M", "phone": "0321-9876543"},
        )
        self.assertEqual(response.status_code, 200)
        patient.refresh_from_db()
        self.assertEqual(patient.phone, "0390-1112223")

    def test_refusal_never_redirects_to_another_site(self):
        self.login(self.owner)
        response = self.client.post(reverse("accounts:clinic_settings"), {}, HTTP_REFERER="https://evil.example/")
        self.assertEqual(response["Location"], "/")
