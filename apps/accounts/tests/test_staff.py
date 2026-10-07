"""Staff list, adding staff, editing roles/access and setting passwords (owner only)."""

from django.conf import settings
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.core.testing import TEST_PASSWORD, make_user

from .base import AccountsTestCase

Role = Membership.Role
Action = AuditLog.Action
NEW_PASSWORD = "Fresh-start-2026"


def membership_of(user, clinic):
    return Membership.objects.get(user=user, clinic=clinic)


class StaffAccessTests(AccountsTestCase):
    """Only the clinic owner may open the staff pages; other clinics' staff are invisible (404)."""

    def staff_urls(self, membership):
        return [
            reverse("accounts:staff_list"),
            reverse("accounts:staff_add"),
            reverse("accounts:staff_edit", args=[membership.pk]),
            reverse("accounts:staff_set_password", args=[membership.pk]),
        ]

    def test_doctor_and_receptionist_get_403(self):
        target = membership_of(self.receptionist, self.clinic)
        for user in (self.doctor, self.receptionist):
            self.login(user)
            for url in self.staff_urls(target):
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        for url in self.staff_urls(membership_of(self.doctor, self.clinic)):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("accounts:login"), response.url)

    def test_other_clinics_staff_are_404(self):
        self.login(self.owner)
        other = membership_of(self.other_owner, self.other_clinic)
        for name in ("accounts:staff_edit", "accounts:staff_set_password"):
            url = reverse(name, args=[other.pk])
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(
                    self.client.post(
                        url, {"role": Role.RECEPTIONIST, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
                    ).status_code,
                    404,
                )
        other.refresh_from_db()
        self.assertEqual(other.role, Role.OWNER)
        self.other_owner.refresh_from_db()
        self.assertTrue(self.other_owner.check_password(TEST_PASSWORD))


class StaffListTests(AccountsTestCase):
    def test_lists_only_this_clinics_staff(self):
        self.login(self.owner)
        response = self.client.get(reverse("accounts:staff_list"))
        self.assertEqual(response.status_code, 200)
        for user in (self.owner, self.doctor, self.receptionist):
            self.assertContains(response, user.email)
        self.assertNotContains(response, self.other_owner.email)
        self.assertContains(response, reverse("accounts:staff_add"))
        self.assertContains(response, "cannot")  # receptionists cannot see clinical notes
        self.assertContains(response, 'data-label="Role"')

    def test_shows_inactive_staff(self):
        make_user(self.clinic, Role.RECEPTIONIST, email="left@example.test", full_name="Zara Left", is_active=False)
        self.login(self.owner)
        response = self.client.get(reverse("accounts:staff_list"))
        self.assertContains(response, "Zara Left")
        self.assertContains(response, "Inactive")


class StaffAddTests(AccountsTestCase):
    url = reverse("accounts:staff_add")

    def form_data(self, **overrides):
        data = {
            "full_name": "Kamran Ali",
            "email": "Kamran@Example.test",
            "role": Role.DOCTOR,
            "title": "Dr.",
            "qualifications": "MBBS",
            "registration_number": "PMDC-555",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
        }
        data.update(overrides)
        return data

    def test_page_renders(self):
        self.login(self.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add staff member")

    def test_adds_new_person_with_account(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data())
        self.assertRedirects(response, reverse("accounts:staff_list"))

        user = User.objects.get(email="kamran@example.test")
        self.assertEqual(user.full_name, "Kamran Ali")
        self.assertTrue(user.check_password(NEW_PASSWORD))
        # The owner knows this password, so it is only for the first sign-in.
        self.assertTrue(user.must_change_password)
        membership = membership_of(user, self.clinic)
        self.assertEqual(membership.role, Role.DOCTOR)
        self.assertEqual(membership.registration_number, "PMDC-555")
        self.assertTrue(membership.is_active)
        self.assertFalse(membership.is_pending)
        self.assertTrue(
            AuditLog.objects.filter(
                action=Action.CREATE, clinic=self.clinic, object_type="Membership", object_id=str(membership.pk)
            ).exists()
        )

    def test_new_person_needs_a_password(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data(password1="", password2=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose a password")
        self.assertFalse(User.objects.filter(email="kamran@example.test").exists())

    def test_password_rules_and_matching(self):
        self.login(self.owner)
        response = self.client.post(self.url, self.form_data(password1="short", password2="short"))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(self.url, self.form_data(password2="Different-2026-x"))
        self.assertContains(response, "The two passwords don&#x27;t match.")
        self.assertFalse(User.objects.filter(email="kamran@example.test").exists())

    def test_existing_account_gets_an_invitation_not_access(self):
        """A doctor who already works at another clinic must accept before this clinic can see them."""
        self.login(self.owner)
        response = self.client.post(
            self.url,
            self.form_data(email="OTHER@example.test", full_name="Someone Else", password1="", password2=""),
            follow=True,
        )
        membership = membership_of(self.other_owner, self.clinic)
        self.assertRedirects(response, reverse("accounts:staff_edit", args=[membership.pk]))
        self.assertContains(response, f"other@example.test already has a {settings.PRODUCT_NAME} account")
        self.assertContains(response, membership.get_invite_url())

        self.assertTrue(membership.is_pending)
        self.assertFalse(membership.is_active)
        self.assertEqual(membership.role, Role.DOCTOR)
        self.assertNotIn(self.other_owner, self.clinic.doctors)
        # Nothing about their account is shown to this clinic, or changed.
        self.assertNotContains(response, "Omar Farooq")
        self.other_owner.refresh_from_db()
        self.assertTrue(self.other_owner.check_password(TEST_PASSWORD))
        self.assertEqual(self.other_owner.full_name, "Omar Farooq")
        self.assertFalse(self.other_owner.must_change_password)
        # Their other clinic is untouched
        self.assertEqual(membership_of(self.other_owner, self.other_clinic).role, Role.OWNER)
        entry = AuditLog.objects.get(action=Action.CREATE, object_type="Membership", object_id=str(membership.pk))
        self.assertEqual(entry.summary, "Invited other@example.test to join as Doctor")
        self.assertEqual(entry.clinic, self.clinic)

    def test_existing_account_password_is_never_changed(self):
        self.login(self.owner)
        self.client.post(self.url, self.form_data(email="other@example.test"))
        self.other_owner.refresh_from_db()
        self.assertTrue(self.other_owner.check_password(TEST_PASSWORD))
        self.assertFalse(self.other_owner.check_password(NEW_PASSWORD))

    def test_already_a_member_is_an_error(self):
        self.login(self.owner)
        before = Membership.objects.count()
        response = self.client.post(self.url, self.form_data(email="Reception@example.test"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hina Malik is already on your staff list")
        self.assertEqual(Membership.objects.count(), before)

    def test_already_invited_is_an_error_that_names_only_the_email(self):
        Membership(user=self.other_owner, clinic=self.clinic, role=Role.DOCTOR).start_invitation()
        self.login(self.owner)
        before = Membership.objects.count()
        response = self.client.post(self.url, self.form_data(email="Other@example.test"))
        self.assertContains(response, "You have already invited other@example.test")
        self.assertNotContains(response, "Omar Farooq")
        self.assertEqual(Membership.objects.count(), before)


class StaffEditTests(AccountsTestCase):
    def url(self, user):
        return reverse("accounts:staff_edit", args=[membership_of(user, self.clinic).pk])

    def form_data(self, **overrides):
        data = {"role": Role.DOCTOR, "title": "Dr.", "qualifications": "", "registration_number": "", "is_active": "on"}
        data.update(overrides)
        return data

    def test_page_renders(self):
        self.login(self.owner)
        response = self.client.get(self.url(self.receptionist))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hina Malik")
        self.assertContains(response, reverse("accounts:staff_set_password", args=[membership_of(self.receptionist, self.clinic).pk]))

    def test_change_role_and_deactivate(self):
        self.login(self.owner)
        response = self.client.post(
            self.url(self.doctor), self.form_data(role=Role.RECEPTIONIST, title="", is_active="")
        )
        self.assertRedirects(response, reverse("accounts:staff_list"))
        membership = membership_of(self.doctor, self.clinic)
        self.assertEqual(membership.role, Role.RECEPTIONIST)
        self.assertFalse(membership.is_active)
        entry = AuditLog.objects.get(action=Action.UPDATE, object_type="Membership", object_id=str(membership.pk))
        self.assertIn("Bilal Hussain", entry.summary)
        self.assertEqual(entry.clinic, self.clinic)

    def test_cannot_demote_the_only_owner(self):
        self.login(self.owner)
        response = self.client.post(self.url(self.owner), self.form_data(role=Role.DOCTOR))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only active owner")
        self.assertEqual(membership_of(self.owner, self.clinic).role, Role.OWNER)

    def test_cannot_deactivate_the_only_owner(self):
        self.login(self.owner)
        response = self.client.post(self.url(self.owner), self.form_data(role=Role.OWNER, is_active=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only active owner")
        self.assertTrue(membership_of(self.owner, self.clinic).is_active)

    def test_inactive_owner_does_not_count(self):
        make_user(self.clinic, Role.OWNER, email="old-owner@example.test", is_active=False)
        self.login(self.owner)
        response = self.client.post(self.url(self.owner), self.form_data(role=Role.DOCTOR))
        self.assertContains(response, "only active owner")

    def test_can_step_down_when_another_owner_exists(self):
        second_owner = make_user(self.clinic, Role.OWNER, email="owner2@example.test")
        self.login(self.owner)
        response = self.client.post(self.url(self.owner), self.form_data(role=Role.DOCTOR))
        # No longer an owner, so the staff pages are closed to them now
        self.assertRedirects(response, reverse("core:dashboard"), fetch_redirect_response=False)
        self.assertEqual(membership_of(self.owner, self.clinic).role, Role.DOCTOR)
        self.assertEqual(membership_of(second_owner, self.clinic).role, Role.OWNER)

    def test_owner_can_promote_someone(self):
        self.login(self.owner)
        self.client.post(self.url(self.doctor), self.form_data(role=Role.OWNER))
        self.assertEqual(membership_of(self.doctor, self.clinic).role, Role.OWNER)


class StaffSetPasswordTests(AccountsTestCase):
    def url(self, user, clinic=None):
        return reverse("accounts:staff_set_password", args=[membership_of(user, clinic or self.clinic).pk])

    def post_password(self, user):
        return self.client.post(self.url(user), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})

    def test_page_renders(self):
        self.login(self.owner)
        response = self.client.get(self.url(self.receptionist))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set a new password")

    def test_sets_password_for_staff_only_at_this_clinic(self):
        self.login(self.owner)
        response = self.post_password(self.receptionist)
        self.assertRedirects(response, reverse("accounts:staff_list"))
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password(NEW_PASSWORD))
        # The owner knows it now, so the receptionist must choose their own at the next sign-in.
        self.assertTrue(self.receptionist.must_change_password)
        self.assertTrue(
            AuditLog.objects.filter(
                action=Action.UPDATE, clinic=self.clinic, summary="Reset password for Hina Malik"
            ).exists()
        )

    def test_refused_when_person_works_at_another_clinic(self):
        Membership.objects.create(user=self.receptionist, clinic=self.other_clinic, role=Role.RECEPTIONIST)
        self.login(self.owner)
        response = self.client.get(self.url(self.receptionist))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "another clinic", status_code=403)

        response = self.post_password(self.receptionist)
        self.assertEqual(response.status_code, 403)
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password(TEST_PASSWORD))

    def test_refused_even_if_other_clinic_access_is_switched_off(self):
        Membership.objects.create(
            user=self.receptionist, clinic=self.other_clinic, role=Role.RECEPTIONIST, is_active=False
        )
        self.login(self.owner)
        self.assertEqual(self.post_password(self.receptionist).status_code, 403)

    def test_refused_while_another_clinic_has_invited_them(self):
        Membership(user=self.receptionist, clinic=self.other_clinic, role=Role.RECEPTIONIST).start_invitation()
        self.login(self.owner)
        self.assertEqual(self.post_password(self.receptionist).status_code, 403)
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password(TEST_PASSWORD))

    def test_refused_for_someone_who_has_not_accepted_your_invitation(self):
        """Even an account with no other clinic: until they accept, it isn't this clinic's to control."""
        loner = User.objects.create_user(email="loner@example.test", password=TEST_PASSWORD, full_name="Lone Person")
        Membership(user=loner, clinic=self.clinic, role=Role.DOCTOR).start_invitation()
        self.login(self.owner)
        for response in (self.client.get(self.url(loner)), self.post_password(loner)):
            self.assertEqual(response.status_code, 403)
            self.assertContains(response, "hasn't accepted your invitation", status_code=403)
            self.assertNotContains(response, "Lone Person", status_code=403)
        loner.refresh_from_db()
        self.assertTrue(loner.check_password(TEST_PASSWORD))

    def test_refused_for_platform_admin_accounts(self):
        User.objects.filter(pk=self.doctor.pk).update(is_staff=True)
        self.login(self.owner)
        self.assertEqual(self.post_password(self.doctor).status_code, 403)

    def test_own_password_goes_to_change_password_page(self):
        self.login(self.owner)
        response = self.client.get(self.url(self.owner))
        self.assertRedirects(response, reverse("accounts:password_change"))

    def test_passwords_must_match(self):
        self.login(self.owner)
        response = self.client.post(
            self.url(self.receptionist), {"new_password1": NEW_PASSWORD, "new_password2": "Not-the-same-2026"}
        )
        self.assertEqual(response.status_code, 200)
        self.receptionist.refresh_from_db()
        self.assertTrue(self.receptionist.check_password(TEST_PASSWORD))
