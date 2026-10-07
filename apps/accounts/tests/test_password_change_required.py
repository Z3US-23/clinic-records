"""A password a clinic owner chose is only for the first sign-in: the person must pick their own."""

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.testing import TEST_PASSWORD

from .base import AccountsTestCase

PASSWORD_CHANGE_URL = reverse("accounts:password_change")
OWN_PASSWORD = "My-own-secret-2026"

MIDDLEWARE = "apps.accounts.middleware.PasswordChangeRequiredMiddleware"


class PasswordChangeRequiredTests(AccountsTestCase):
    def setUp(self):
        cache.clear()

    def test_middleware_is_switched_on_after_sign_in_and_messages(self):
        middleware = settings.MIDDLEWARE
        self.assertIn(MIDDLEWARE, middleware)
        position = middleware.index(MIDDLEWARE)
        for needed_first in (
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ):
            self.assertLess(middleware.index(needed_first), position)

    def flag(self, user):
        User.objects.filter(pk=user.pk).update(must_change_password=True)
        user.refresh_from_db()

    def change_own_password(self):
        return self.client.post(
            PASSWORD_CHANGE_URL,
            {"old_password": TEST_PASSWORD, "new_password1": OWN_PASSWORD, "new_password2": OWN_PASSWORD},
        )

    def test_every_page_sends_them_to_change_password(self):
        self.flag(self.doctor)
        self.login(self.doctor)
        for name in ("core:dashboard", "patients:list", "accounts:profile", "appointments:day"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertRedirects(response, PASSWORD_CHANGE_URL, fetch_redirect_response=False)
        response = self.client.get(reverse("patients:list"), follow=True)
        self.assertContains(response, "Please choose your own password before you continue.")

    def test_password_pages_sign_out_and_app_files_still_work(self):
        self.flag(self.doctor)
        self.login(self.doctor)
        self.assertEqual(self.client.get(PASSWORD_CHANGE_URL).status_code, 200)
        self.assertEqual(self.client.get(reverse("core:manifest")).status_code, 200)
        response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)

    def test_after_choosing_their_own_password_everything_opens(self):
        self.flag(self.doctor)
        self.login(self.doctor)
        response = self.change_own_password()
        self.assertRedirects(response, reverse("accounts:password_change_done"))
        self.doctor.refresh_from_db()
        self.assertFalse(self.doctor.must_change_password)
        self.assertEqual(self.client.get(reverse("patients:list")).status_code, 200)

    def test_people_without_the_flag_are_not_affected(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(reverse("patients:list")).status_code, 200)

    def test_new_staff_member_from_first_sign_in_to_own_password(self):
        """The whole journey: owner adds someone, they sign in, they must change the password."""
        self.login(self.owner)
        self.client.post(
            reverse("accounts:staff_add"),
            {
                "full_name": "Kamran Ali",
                "email": "kamran@example.test",
                "role": Membership.Role.RECEPTIONIST,
                "password1": TEST_PASSWORD,
                "password2": TEST_PASSWORD,
            },
        )
        self.client.logout()

        response = self.client.post(
            reverse("accounts:login"), {"username": "kamran@example.test", "password": TEST_PASSWORD}, follow=True
        )
        self.assertRedirects(response, PASSWORD_CHANGE_URL)
        self.assertContains(response, "Choose your own password")

        self.change_own_password()
        self.assertEqual(self.client.get(reverse("patients:list")).status_code, 200)

    def test_owner_reset_makes_them_choose_again(self):
        self.login(self.owner)
        membership = Membership.objects.get(user=self.receptionist, clinic=self.clinic)
        self.client.post(
            reverse("accounts:staff_set_password", args=[membership.pk]),
            {"new_password1": OWN_PASSWORD, "new_password2": OWN_PASSWORD},
        )
        self.client.logout()

        self.client.post(reverse("accounts:login"), {"username": "reception@example.test", "password": OWN_PASSWORD})
        response = self.client.get(reverse("patients:list"))
        self.assertRedirects(response, PASSWORD_CHANGE_URL, fetch_redirect_response=False)
