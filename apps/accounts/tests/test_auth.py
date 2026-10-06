"""Sign in, lockout, sign out, password change and the audit trail for them."""

from unittest import mock

from django.contrib.auth import SESSION_KEY
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.accounts import lockout
from apps.core.models import AuditLog
from apps.core.testing import TEST_PASSWORD

from .base import AccountsTestCase

Action = AuditLog.Action
LOGIN_URL = reverse("accounts:login")
DASHBOARD_URL = reverse("core:dashboard")


class LoginTests(AccountsTestCase):
    def setUp(self):
        cache.clear()  # lockout counters live in the cache

    def sign_in(self, email, password=TEST_PASSWORD, **extra):
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, **extra)

    def test_login_page_renders_with_signup_link(self):
        response = self.client.get(LOGIN_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")
        self.assertContains(response, 'type="email"')
        self.assertContains(response, reverse("accounts:signup"))

    @override_settings(ALLOW_CLINIC_SIGNUP=False)
    def test_no_signup_link_when_signup_closed(self):
        response = self.client.get(LOGIN_URL)
        self.assertNotContains(response, reverse("accounts:signup"))

    def test_login_with_any_letter_case_redirects_to_dashboard(self):
        response = self.sign_in("Owner@Example.TEST")
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)
        self.assertEqual(int(self.client.session[SESSION_KEY]), self.owner.pk)

    def test_login_follows_safe_next(self):
        response = self.client.post(
            f"{LOGIN_URL}?next=/patients/", {"username": "doctor@example.test", "password": TEST_PASSWORD, "next": "/patients/"}
        )
        self.assertRedirects(response, "/patients/", fetch_redirect_response=False)

    def test_login_ignores_unsafe_next(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": "doctor@example.test", "password": TEST_PASSWORD, "next": "https://evil.example/steal"},
        )
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_signed_in_user_is_sent_to_dashboard(self):
        self.login(self.receptionist)
        response = self.client.get(LOGIN_URL)
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_wrong_password_shows_error_and_audits_failure(self):
        response = self.sign_in("owner@example.test", "wrong-password-1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "don&#x27;t match")
        self.assertNotIn(SESSION_KEY, self.client.session)

        entry = AuditLog.objects.get(action=Action.LOGIN_FAILED)
        self.assertIsNone(entry.clinic)
        self.assertEqual(entry.summary, "Failed sign-in for owner@example.test")
        self.assertNotIn("wrong-password-1", entry.summary)
        self.assertEqual(entry.ip_address, "127.0.0.1")

    def test_successful_login_is_audited_with_clinic(self):
        self.sign_in("doctor@example.test")
        entry = AuditLog.objects.filter(action=Action.LOGIN).latest("created_at")
        self.assertEqual(entry.user, self.doctor)
        self.assertEqual(entry.clinic, self.clinic)
        self.assertEqual(entry.summary, "Signed in")


@override_settings(LOGIN_MAX_ATTEMPTS=3, LOGIN_LOCKOUT_MINUTES=15)
class LockoutTests(AccountsTestCase):
    def setUp(self):
        cache.clear()

    def sign_in(self, password, email="owner@example.test", ip="127.0.0.1"):
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, REMOTE_ADDR=ip)

    def fail(self, times, **kwargs):
        for _ in range(times):
            self.sign_in("wrong-password-1", **kwargs)

    def test_locked_after_max_failures_even_with_right_password(self):
        self.fail(3)
        failures_logged = AuditLog.objects.filter(action=Action.LOGIN_FAILED).count()

        with mock.patch("django.contrib.auth.forms.authenticate") as fake_authenticate:
            response = self.sign_in(TEST_PASSWORD)
        fake_authenticate.assert_not_called()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed attempts. Please wait 15 minutes and try again.")
        self.assertNotIn(SESSION_KEY, self.client.session)
        # authenticate() was not called, so no new failed-login entry either
        self.assertEqual(AuditLog.objects.filter(action=Action.LOGIN_FAILED).count(), failures_logged)

    def test_lock_is_per_email(self):
        self.fail(3)
        response = self.sign_in(TEST_PASSWORD, email="doctor@example.test")
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_lock_is_per_ip(self):
        self.fail(3, ip="10.0.0.1")
        response = self.sign_in(TEST_PASSWORD, ip="10.0.0.2")
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_lock_ignores_email_letter_case(self):
        self.fail(3, email="OWNER@example.test")
        response = self.sign_in(TEST_PASSWORD, email="owner@example.test")
        self.assertContains(response, "Too many failed attempts")

    def test_success_clears_the_count(self):
        self.fail(2)
        self.assertRedirects(self.sign_in(TEST_PASSWORD), DASHBOARD_URL, fetch_redirect_response=False)
        self.client.logout()
        self.fail(2)
        response = self.sign_in(TEST_PASSWORD)
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_lock_ends_after_the_wait(self):
        start = 1_000_000.0
        with mock.patch("apps.accounts.lockout._now", return_value=start):
            self.fail(3)
            self.assertContains(self.sign_in(TEST_PASSWORD), "Please wait 15 minutes")
        with mock.patch("apps.accounts.lockout._now", return_value=start + 14 * 60 + 30):
            self.assertContains(self.sign_in(TEST_PASSWORD), "Please wait 1 minute and")
        with mock.patch("apps.accounts.lockout._now", return_value=start + 15 * 60 + 1):
            response = self.sign_in(TEST_PASSWORD)
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

    def test_failures_outside_the_window_do_not_add_up(self):
        start = 1_000_000.0
        with mock.patch("apps.accounts.lockout._now", return_value=start):
            self.fail(2)
        with mock.patch("apps.accounts.lockout._now", return_value=start + 16 * 60):
            self.fail(1)
            self.assertEqual(lockout.minutes_locked("owner@example.test", "127.0.0.1"), 0)


class LogoutTests(AccountsTestCase):
    def test_get_is_not_allowed(self):
        self.login(self.doctor)
        response = self.client.get(reverse("accounts:logout"))
        self.assertEqual(response.status_code, 405)
        self.assertIn(SESSION_KEY, self.client.session)

    def test_post_signs_out_with_message_and_audit(self):
        self.login(self.doctor)
        response = self.client.post(reverse("accounts:logout"), follow=True)
        self.assertRedirects(response, LOGIN_URL)
        self.assertContains(response, "You have signed out.")
        self.assertNotIn(SESSION_KEY, self.client.session)

        entry = AuditLog.objects.filter(action=Action.LOGOUT).get()
        self.assertEqual(entry.user, self.doctor)
        self.assertEqual(entry.clinic, self.clinic)


class PasswordChangeTests(AccountsTestCase):
    url = reverse("accounts:password_change")

    def test_requires_sign_in(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{LOGIN_URL}?next={self.url}", fetch_redirect_response=False)

    def test_page_renders_for_every_role(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(user=user.email):
                self.login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Change your password")

    def test_change_password(self):
        self.login(self.receptionist)
        response = self.client.post(
            self.url,
            {
                "old_password": TEST_PASSWORD,
                "new_password1": "Brand-new-secret-77",
                "new_password2": "Brand-new-secret-77",
            },
        )
        self.assertRedirects(response, reverse("accounts:password_change_done"))
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password("Brand-new-secret-77"))
        self.assertTrue(
            AuditLog.objects.filter(action=Action.UPDATE, user=self.receptionist, summary="Changed own password").exists()
        )
        # Still signed in on this device
        self.assertEqual(self.client.get(reverse("accounts:password_change_done")).status_code, 200)

    def test_wrong_current_password_is_refused(self):
        self.login(self.receptionist)
        response = self.client.post(
            self.url,
            {"old_password": "nope", "new_password1": "Brand-new-secret-77", "new_password2": "Brand-new-secret-77"},
        )
        self.assertEqual(response.status_code, 200)
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password(TEST_PASSWORD))
