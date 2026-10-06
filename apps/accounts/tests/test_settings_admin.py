"""Clinic settings (owner only) and the platform admin pages."""

from django.urls import reverse

from apps.accounts.models import Clinic, Membership, User
from apps.core.models import AuditLog

from .base import AccountsTestCase


class ClinicSettingsTests(AccountsTestCase):
    url = reverse("accounts:clinic_settings")

    def form_data(self, **overrides):
        data = {
            "name": "Al-Noor Family Clinic",
            "address": "12 Main Boulevard, Gulberg",
            "city": "Lahore",
            "phone": "0300-1234567",
            "email": "hello@alnoor.example",
            "country": "PK",
            "timezone": "Asia/Karachi",
            "appointment_reminder_days": 2,
            "followup_reminder_days": 3,
            "overdue_grace_days": 7,
            "default_appointment_minutes": 20,
            "prescription_header": "Mon–Sat, 5 pm to 10 pm",
            "prescription_footer": "Please bring this prescription on your next visit.",
        }
        data.update(overrides)
        return data

    def test_owner_sees_all_sections(self):
        self.login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for heading in ("Clinic details", "Reminders", "Printed prescriptions"):
            self.assertContains(response, heading)

    def test_doctor_and_receptionist_get_403(self):
        for user in (self.doctor, self.receptionist):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.client.post(self.url, self.form_data(name="Hacked")).status_code, 403)
        self.clinic.refresh_from_db()
        self.assertEqual(self.clinic.name, "Al-Noor Family Clinic")

    def test_owner_saves_settings_for_own_clinic_only(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data())
        self.assertRedirects(response, self.url)
        self.clinic.refresh_from_db()
        self.assertEqual(self.clinic.city, "Lahore")
        self.assertEqual(self.clinic.appointment_reminder_days, 2)
        self.assertEqual(self.clinic.default_appointment_minutes, 20)
        self.assertEqual(self.clinic.prescription_header, "Mon–Sat, 5 pm to 10 pm")

        self.other_clinic.refresh_from_db()
        self.assertEqual(self.other_clinic.city, "")
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, object_type="Clinic")
        self.assertEqual(entry.clinic, self.clinic)
        self.assertIn("appointment_reminder_days", entry.summary)

    def test_number_limits(self):
        self.login(self.owner)
        cases = {
            "appointment_reminder_days": 15,
            "followup_reminder_days": -1,
            "overdue_grace_days": 61,
            "default_appointment_minutes": 4,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                response = self.client.post(self.url, self.form_data(**{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
        self.clinic.refresh_from_db()
        self.assertEqual(self.clinic.appointment_reminder_days, 1)

    def test_unreadable_phone_refused(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data(phone="abc"))
        self.assertIn("phone", response.context["form"].errors)

    def test_invalid_post_does_not_change_the_page_header_name(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data(name="Renamed", default_appointment_minutes=1))
        self.assertEqual(response.context["current_clinic"].name, "Al-Noor Family Clinic")


class AdminTests(AccountsTestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email="admin@platform.example", password="Admin-pass-2026", full_name="Platform Admin"
        )
        self.client.force_login(self.admin_user)

    def test_admin_pages_render(self):
        membership = Membership.objects.get(user=self.doctor)
        urls = [
            reverse("admin:accounts_user_changelist") + "?q=doctor",
            reverse("admin:accounts_user_add"),
            reverse("admin:accounts_user_change", args=[self.doctor.pk]),
            reverse("admin:accounts_clinic_changelist") + "?q=Noor",
            reverse("admin:accounts_clinic_add"),
            reverse("admin:accounts_clinic_change", args=[self.clinic.pk]),
            reverse("admin:accounts_membership_changelist"),
            reverse("admin:accounts_membership_change", args=[membership.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_creates_user_with_email(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": "New.Person@Example.test",
                "full_name": "New Person",
                "usable_password": "true",
                "password1": "Admin-made-2026",
                "password2": "Admin-made-2026",
                "memberships-TOTAL_FORMS": 0,
                "memberships-INITIAL_FORMS": 0,
            },
        )
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email="new.person@example.test")
        self.assertTrue(user.check_password("Admin-made-2026"))

    def test_admin_creates_clinic_with_slug(self):
        response = self.client.post(
            reverse("admin:accounts_clinic_add"),
            {
                "name": "City Clinic",
                "is_active": "on",
                "country": "IN",
                "timezone": "Asia/Kolkata",
                "appointment_reminder_days": 1,
                "followup_reminder_days": 2,
                "overdue_grace_days": 3,
                "default_appointment_minutes": 15,
                "memberships-TOTAL_FORMS": 0,
                "memberships-INITIAL_FORMS": 0,
            },
        )
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context.get("errors"))
        self.assertEqual(Clinic.objects.get(name="City Clinic").slug, "city-clinic")
