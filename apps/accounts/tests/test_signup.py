"""Registering a new clinic."""

from django.contrib.auth import SESSION_KEY
from django.test import override_settings
from django.urls import reverse

from apps.accounts.models import Clinic, Membership, User
from apps.core.models import AuditLog

from .base import AccountsTestCase

SIGNUP_URL = reverse("accounts:signup")


class SignupTests(AccountsTestCase):
    def form_data(self, **overrides):
        data = {
            "clinic_name": "Shifa Health Centre",
            "country": "PK",
            "city": "Lahore",
            "clinic_phone": "042-35761234",
            "full_name": "Ayesha Khan",
            "title": "Dr.",
            "email": "Ayesha@Shifa.Example",
            "password1": "Clinic-secret-2026",
            "password2": "Clinic-secret-2026",
        }
        data.update(overrides)
        return data

    def test_page_renders(self):
        response = self.client.get(SIGNUP_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Register your clinic")
        self.assertContains(response, 'value="Dr."')

    @override_settings(ALLOW_CLINIC_SIGNUP=False)
    def test_404_when_signup_is_closed(self):
        self.assertEqual(self.client.get(SIGNUP_URL).status_code, 404)
        self.assertEqual(self.client.post(SIGNUP_URL, self.form_data()).status_code, 404)
        self.assertFalse(Clinic.objects.filter(name="Shifa Health Centre").exists())

    def test_signed_in_user_is_sent_to_dashboard(self):
        self.login(self.doctor)
        response = self.client.get(SIGNUP_URL)
        self.assertRedirects(response, reverse("core:dashboard"), fetch_redirect_response=False)

    def test_creates_clinic_owner_and_signs_in(self):
        response = self.client.post(SIGNUP_URL, self.form_data())
        self.assertRedirects(response, reverse("core:dashboard"), fetch_redirect_response=False)

        clinic = Clinic.objects.get(name="Shifa Health Centre")
        self.assertEqual(clinic.country, "PK")
        self.assertEqual(clinic.timezone, "Asia/Karachi")
        self.assertEqual(clinic.city, "Lahore")
        self.assertEqual(clinic.phone, "042-35761234")

        user = User.objects.get(email="ayesha@shifa.example")
        self.assertEqual(user.full_name, "Ayesha Khan")
        self.assertTrue(user.check_password("Clinic-secret-2026"))
        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.clinic, clinic)
        self.assertEqual(membership.role, Membership.Role.OWNER)
        self.assertEqual(membership.title, "Dr.")

        self.assertEqual(int(self.client.session[SESSION_KEY]), user.pk)
        self.assertEqual(self.client.session["clinic_id"], clinic.pk)
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.CREATE, clinic=clinic, user=user, summary="Clinic registered"
            ).exists()
        )

    def test_india_gets_india_time(self):
        self.client.post(SIGNUP_URL, self.form_data(country="IN", clinic_phone="98765 43210"))
        clinic = Clinic.objects.get(name="Shifa Health Centre")
        self.assertEqual(clinic.country, "IN")
        self.assertEqual(clinic.timezone, "Asia/Kolkata")

    def test_email_already_used_any_case(self):
        response = self.client.post(SIGNUP_URL, self.form_data(email="DOCTOR@example.test"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertFalse(Clinic.objects.filter(name="Shifa Health Centre").exists())

    def test_weak_password_refused(self):
        response = self.client.post(SIGNUP_URL, self.form_data(password1="12345678", password2="12345678"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="ayesha@shifa.example").exists())

    def test_passwords_must_match(self):
        response = self.client.post(SIGNUP_URL, self.form_data(password2="Something-else-2026"))
        self.assertContains(response, "The two passwords don&#x27;t match.")
        self.assertFalse(Clinic.objects.filter(name="Shifa Health Centre").exists())

    def test_unreadable_phone_refused(self):
        response = self.client.post(SIGNUP_URL, self.form_data(clinic_phone="12"))
        self.assertContains(response, "Enter a phone number we can understand")
        self.assertFalse(Clinic.objects.filter(name="Shifa Health Centre").exists())
