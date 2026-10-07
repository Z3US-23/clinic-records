"""Sign in (app and Django admin), lockout, sign out, password change and the audit trail for them."""

from unittest import mock

from django.contrib.auth import SESSION_KEY
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.accounts import lockout
from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.core.testing import TEST_PASSWORD, make_clinic

from .base import AccountsTestCase

Action = AuditLog.Action
LOGIN_URL = reverse("accounts:login")
ADMIN_LOGIN_URL = reverse("admin:login")
DASHBOARD_URL = reverse("core:dashboard")


class LoginTests(AccountsTestCase):
    def setUp(self):
        cache.clear()  # lockout counters live in the cache

    def sign_in(self, email, password=TEST_PASSWORD, **extra):
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, **extra)

    @override_settings(ALLOW_CLINIC_SIGNUP=True)
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

        # The owner of the account's clinic can see the attempt in their audit log.
        entry = AuditLog.objects.get(action=Action.LOGIN_FAILED)
        self.assertEqual(entry.clinic, self.clinic)
        self.assertEqual(entry.user, self.owner)
        self.assertEqual(entry.summary, "Failed sign-in for owner@example.test")
        self.assertNotIn("wrong-password-1", entry.summary)
        self.assertEqual(entry.ip_address, "127.0.0.1")

    def test_successful_login_is_audited_with_clinic(self):
        self.sign_in("doctor@example.test")
        entry = AuditLog.objects.filter(action=Action.LOGIN).latest("created_at")
        self.assertEqual(entry.user, self.doctor)
        self.assertEqual(entry.clinic, self.clinic)
        self.assertEqual(entry.summary, "Signed in")


class FailedSignInAuditTests(AccountsTestCase):
    """A wrong password shows up in the audit log of every clinic the targeted account works at."""

    def setUp(self):
        cache.clear()

    def fail_sign_in(self, email):
        response = self.client.post(LOGIN_URL, {"username": email, "password": "wrong-password-1"})
        self.assertEqual(response.status_code, 200)

    def test_unknown_email_gets_one_entry_without_a_clinic(self):
        self.fail_sign_in("nobody@example.test")
        entry = AuditLog.objects.get(action=Action.LOGIN_FAILED)
        self.assertIsNone(entry.clinic)
        self.assertIsNone(entry.user)
        self.assertEqual(entry.summary, "Failed sign-in for nobody@example.test")

    def test_one_entry_per_active_clinic(self):
        Membership.objects.create(user=self.doctor, clinic=self.other_clinic, role=Membership.Role.DOCTOR)
        switched_off = make_clinic("Old Clinic")
        Membership.objects.create(user=self.doctor, clinic=switched_off, role=Membership.Role.DOCTOR, is_active=False)
        invited_to = make_clinic("Inviting Clinic")
        Membership(user=self.doctor, clinic=invited_to, role=Membership.Role.DOCTOR).start_invitation()

        self.fail_sign_in("Doctor@Example.test")

        entries = AuditLog.objects.filter(action=Action.LOGIN_FAILED)
        self.assertEqual({entry.clinic for entry in entries}, {self.clinic, self.other_clinic})
        self.assertEqual({entry.user for entry in entries}, {self.doctor})

    def test_owner_sees_their_staffs_failed_sign_ins_and_no_one_elses(self):
        self.fail_sign_in("reception@example.test")
        audit_url = reverse("core:audit_log") + "?action=login_failed"

        self.login(self.owner)
        self.assertContains(self.client.get(audit_url), "Failed sign-in for reception@example.test")

        self.login(self.other_owner)
        self.assertNotContains(self.client.get(audit_url), "reception@example.test")


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


@override_settings(
    LOGIN_MAX_ATTEMPTS=3, LOGIN_MAX_ATTEMPTS_PER_EMAIL=6, LOGIN_MAX_ATTEMPTS_PER_IP=5, LOGIN_LOCKOUT_MINUTES=15
)
class WiderLockoutTests(AccountsTestCase):
    """Changing address, or trying many accounts from one address, doesn't get around the lock."""

    def setUp(self):
        cache.clear()

    def sign_in(self, email, password, ip):
        return self.client.post(LOGIN_URL, {"username": email, "password": password}, REMOTE_ADDR=ip)

    def assertSignedIn(self, response):
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)
        self.client.logout()

    def assertLocked(self, response):
        self.assertContains(response, "Too many failed attempts")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_one_account_guessed_from_many_addresses(self):
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            for _ in range(2):  # stays under the per-address limit each time
                self.sign_in("owner@example.test", "wrong-password-1", ip)
        self.assertLocked(self.sign_in("owner@example.test", TEST_PASSWORD, "10.0.0.4"))
        # Other accounts are not affected.
        self.assertSignedIn(self.sign_in("doctor@example.test", TEST_PASSWORD, "10.0.0.4"))

    def test_many_accounts_tried_from_one_address(self):
        for n in range(5):
            self.sign_in(f"guess{n}@example.test", "wrong-password-1", "10.0.0.9")
        self.assertLocked(self.sign_in("doctor@example.test", TEST_PASSWORD, "10.0.0.9"))
        # The same account from another address is fine.
        self.assertSignedIn(self.sign_in("doctor@example.test", TEST_PASSWORD, "10.0.0.10"))

    def test_ipv6_addresses_in_the_same_64_count_together(self):
        for ip in ("2001:db8:1:1::1", "2001:db8:1:1::2", "2001:db8:1:1:abcd::3"):
            self.sign_in("owner@example.test", "wrong-password-1", ip)
        self.assertLocked(self.sign_in("owner@example.test", TEST_PASSWORD, "2001:db8:1:1::99"))
        # Another /64 network is another place.
        self.assertSignedIn(self.sign_in("owner@example.test", TEST_PASSWORD, "2001:db8:1:2::1"))

    def test_ipv4_visitors_seen_through_ipv6_are_counted_by_their_own_address(self):
        for _ in range(3):
            self.sign_in("owner@example.test", "wrong-password-1", "::ffff:10.0.0.1")
        self.assertLocked(self.sign_in("owner@example.test", TEST_PASSWORD, "10.0.0.1"))
        self.assertSignedIn(self.sign_in("owner@example.test", TEST_PASSWORD, "::ffff:10.0.0.2"))

    def test_signing_in_to_your_own_account_does_not_reset_the_address_count(self):
        for n in range(4):
            self.sign_in(f"guess{n}@example.test", "wrong-password-1", "10.0.0.9")
        self.assertSignedIn(self.sign_in("doctor@example.test", TEST_PASSWORD, "10.0.0.9"))
        self.sign_in("guess9@example.test", "wrong-password-1", "10.0.0.9")  # 5th failure from this address
        self.assertLocked(self.sign_in("doctor@example.test", TEST_PASSWORD, "10.0.0.9"))


@override_settings(LOGIN_MAX_ATTEMPTS=3, LOGIN_LOCKOUT_MINUTES=15)
class AdminLoginLockoutTests(AccountsTestCase):
    """The Django admin's sign-in page has the same lock: its accounts can read every clinic's records."""

    admin_password = "Admin-pass-2026"

    def setUp(self):
        cache.clear()
        self.admin_user = User.objects.create_superuser(
            email="admin@platform.example", password=self.admin_password, full_name="Platform Admin"
        )

    def admin_sign_in(self, password, email="admin@platform.example"):
        return self.client.post(ADMIN_LOGIN_URL, {"username": email, "password": password, "next": "/admin/"})

    def app_sign_in(self, password, email="admin@platform.example"):
        return self.client.post(LOGIN_URL, {"username": email, "password": password})

    def assertLocked(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Too many failed attempts. Please wait 15 minutes and try again.")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_locked_after_max_failures_even_with_right_password(self):
        for _ in range(3):
            self.admin_sign_in("wrong-password-1")
        self.assertLocked(self.admin_sign_in(self.admin_password))
        # Still signed out: the admin sends us back to its sign-in page.
        response = self.client.get("/admin/")
        self.assertRedirects(response, f"{ADMIN_LOGIN_URL}?next=/admin/", fetch_redirect_response=False)

    def test_failures_on_the_app_sign_in_page_lock_the_admin_too(self):
        for _ in range(3):
            self.app_sign_in("wrong-password-1")
        self.assertLocked(self.admin_sign_in(self.admin_password))

    def test_failures_on_the_admin_lock_the_app_sign_in_page_too(self):
        for _ in range(2):
            self.admin_sign_in("wrong-password-1")
        self.app_sign_in("wrong-password-1")  # the 3rd failure, on the other page
        self.assertLocked(self.app_sign_in(self.admin_password))

    def test_success_clears_the_count_and_email_case_is_ignored(self):
        for _ in range(2):
            self.admin_sign_in("wrong-password-1")
        response = self.admin_sign_in(self.admin_password, email="ADMIN@Platform.example")
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.client.logout()
        for _ in range(2):
            self.admin_sign_in("wrong-password-1")
        self.assertRedirects(self.admin_sign_in(self.admin_password), "/admin/", fetch_redirect_response=False)

    def test_clinic_staff_cannot_use_the_admin(self):
        response = self.admin_sign_in(TEST_PASSWORD, email="owner@example.test")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)


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

    def test_choosing_your_own_password_clears_the_must_change_flag(self):
        User.objects.filter(pk=self.receptionist.pk).update(must_change_password=True)
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertContains(response, "Choose your own password")
        self.assertContains(response, "Your clinic owner chose the password")

        self.client.post(
            self.url,
            {
                "old_password": TEST_PASSWORD,
                "new_password1": "Brand-new-secret-77",
                "new_password2": "Brand-new-secret-77",
            },
        )
        self.receptionist.refresh_from_db()
        self.assertFalse(self.receptionist.must_change_password)
        self.assertTrue(self.receptionist.check_password("Brand-new-secret-77"))
