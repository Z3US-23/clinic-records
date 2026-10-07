"""Inviting someone who already has an account, and their side: accepting or declining.

The attack these tests guard against: clinic A's owner knows the password of a doctor's
account (they created it, or they squatted the doctor's email). Clinic B adds that email.
Before invitations, B's records opened to anyone with that password. Now B's owner gets a
join link that only works for that account, signed in, with the password typed again,
and B gives the link to the real doctor in person or on WhatsApp.
"""

from datetime import datetime, timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.core.testing import TEST_PASSWORD

from .base import AccountsTestCase

Role = Membership.Role
Action = AuditLog.Action
DASHBOARD_URL = reverse("core:dashboard")


class InvitationTestCase(AccountsTestCase):
    """self.other_owner (who runs the other clinic) is invited to work at self.clinic as a doctor."""

    def setUp(self):
        cache.clear()  # wrong passwords on the join page count towards the sign-in lockout
        self.invite = Membership(user=self.other_owner, clinic=self.clinic, role=Role.DOCTOR, title="Dr.")
        self.invite.start_invitation()

    def join_url(self, membership=None):
        return reverse("accounts:join", args=[(membership or self.invite).invite_token])

    def accept(self, password=TEST_PASSWORD, url=None):
        return self.client.post(url or self.join_url(), {"action": "accept", "password": password})

    def reload_invite(self):
        return Membership.objects.filter(pk=self.invite.pk).first()


class PendingInvitationIsolationTests(InvitationTestCase):
    """Until they accept, the invited account gets nothing from this clinic."""

    def test_cannot_switch_to_the_clinic(self):
        self.login(self.other_owner)
        response = self.client.post(reverse("accounts:switch_clinic", args=[self.clinic.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.session["clinic_id"], self.other_clinic.pk)

    def test_cannot_open_the_clinics_patients(self):
        patient = self.make_patient(allergies="Penicillin")
        self.login(self.other_owner)
        response = self.client.get(reverse("patients:detail", args=[patient.pk]))
        self.assertEqual(response.status_code, 404)

    def test_not_in_the_clinic_switcher_or_doctor_list(self):
        self.login(self.other_owner)
        response = self.client.get(reverse("accounts:profile"))
        self.assertEqual(response.context["current_clinic"], self.other_clinic)
        self.assertEqual([m.clinic for m in response.context["user_clinics"]], [self.other_clinic])
        self.assertNotIn(self.other_owner, self.clinic.doctors)

    def test_a_user_whose_only_membership_is_an_invitation_has_no_clinic(self):
        newcomer = User.objects.create_user(email="new@example.test", password=TEST_PASSWORD, full_name="New Comer")
        Membership(user=newcomer, clinic=self.clinic, role=Role.DOCTOR).start_invitation()
        self.login(newcomer)
        self.assertRedirects(self.client.get(DASHBOARD_URL), reverse("accounts:no_clinic"))

    def test_saving_a_pending_membership_never_switches_it_on(self):
        self.invite.is_active = True
        self.invite.save()
        self.assertFalse(self.reload_invite().is_active)


class OwnerSideTests(InvitationTestCase):
    """What the inviting owner sees and can do. They never see the account's name or last sign-in."""

    def test_staff_list_shows_the_invitation_by_email_only(self):
        last_seen = timezone.make_aware(datetime(2025, 1, 15, 12, 0))
        User.objects.filter(pk=self.other_owner.pk).update(last_login=last_seen)
        self.login(self.owner)
        response = self.client.get(reverse("accounts:staff_list"))
        self.assertContains(response, "Waiting for them to accept")
        self.assertContains(response, "other@example.test")
        self.assertNotContains(response, "Omar Farooq")
        self.assertNotContains(response, "15 Jan 2025")  # their account's last sign-in

    def test_staff_edit_shows_the_join_link_and_no_access_switch(self):
        self.login(self.owner)
        response = self.client.get(reverse("accounts:staff_edit", args=[self.invite.pk]))
        self.assertContains(response, self.invite.get_invite_url())
        self.assertContains(response, "https://wa.me/?text=")
        self.assertNotContains(response, 'name="is_active"')
        self.assertNotContains(response, "Omar Farooq")
        self.assertNotContains(response, reverse("accounts:staff_set_password", args=[self.invite.pk]))

    def test_staff_edit_cannot_switch_on_an_invitation(self):
        self.login(self.owner)
        response = self.client.post(
            reverse("accounts:staff_edit", args=[self.invite.pk]),
            {
                "role": Role.OWNER,
                "title": "Dr.",
                "qualifications": "MBBS",
                "registration_number": "",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("accounts:staff_list"))
        invite = self.reload_invite()
        self.assertEqual(invite.role, Role.OWNER)  # the role can be changed before they accept...
        self.assertTrue(invite.is_pending)  # ...but access only comes from accepting
        self.assertFalse(invite.is_active)
        entry = AuditLog.objects.get(action=Action.UPDATE, object_type="Membership")
        self.assertIn("other@example.test", entry.summary)
        self.assertNotIn("Omar Farooq", entry.summary)

    def test_expired_link_is_not_shown(self):
        Membership.objects.filter(pk=self.invite.pk).update(invited_at=timezone.now() - timedelta(days=8))
        self.login(self.owner)
        response = self.client.get(reverse("accounts:staff_edit", args=[self.invite.pk]))
        self.assertContains(response, "This join link has expired")
        self.assertNotContains(response, self.invite.invite_token)


class StaffInviteActionTests(InvitationTestCase):
    """accounts:staff_invite: an owner makes a new link or cancels the invitation (POST only)."""

    def url(self, membership=None):
        return reverse("accounts:staff_invite", args=[(membership or self.invite).pk])

    def test_make_a_new_link(self):
        old_url = self.join_url()
        Membership.objects.filter(pk=self.invite.pk).update(invited_at=timezone.now() - timedelta(days=8))
        self.login(self.owner)
        response = self.client.post(self.url(), {"action": "renew"})
        self.assertRedirects(response, reverse("accounts:staff_edit", args=[self.invite.pk]))
        invite = self.reload_invite()
        self.assertFalse(invite.invite_expired)
        self.assertNotEqual(self.join_url(invite), old_url)
        self.assertTrue(
            AuditLog.objects.filter(action=Action.UPDATE, summary="Made a new join link for other@example.test").exists()
        )

        # The old link stops working.
        self.login(self.other_owner)
        self.assertEqual(self.client.get(old_url).status_code, 404)
        self.assertEqual(self.client.get(self.join_url(invite)).status_code, 200)

    def test_cancel(self):
        self.login(self.owner)
        response = self.client.post(self.url(), {"action": "cancel"})
        self.assertRedirects(response, reverse("accounts:staff_list"))
        self.assertIsNone(self.reload_invite())
        self.assertTrue(
            AuditLog.objects.filter(
                action=Action.DELETE, clinic=self.clinic, summary="Cancelled the invitation for other@example.test"
            ).exists()
        )

    def test_post_only(self):
        self.login(self.owner)
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_owner_only(self):
        for user in (self.doctor, self.receptionist):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.post(self.url(), {"action": "cancel"}).status_code, 403)
        self.assertIsNotNone(self.reload_invite())

    def test_other_clinics_invitations_are_404(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.post(self.url(), {"action": "cancel"}).status_code, 404)
        self.assertIsNotNone(self.reload_invite())

    def test_accepted_staff_are_404(self):
        """Nobody can be 'uninvited' (deleted) once they have joined."""
        self.login(self.owner)
        member = Membership.objects.get(user=self.doctor, clinic=self.clinic)
        self.assertEqual(self.client.post(self.url(member), {"action": "cancel"}).status_code, 404)
        self.assertTrue(Membership.objects.filter(pk=member.pk).exists())

    def test_anonymous_redirected_to_login(self):
        response = self.client.post(self.url(), {"action": "cancel"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


class JoinTests(InvitationTestCase):
    """accounts:join: the invited person accepts or declines."""

    def test_requires_sign_in(self):
        url = self.join_url()
        response = self.client.get(url)
        self.assertRedirects(response, f"{reverse('accounts:login')}?next={url}", fetch_redirect_response=False)

    def test_shows_the_invitation(self):
        self.login(self.other_owner)
        response = self.client.get(self.join_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Join Al-Noor Family Clinic")
        self.assertContains(response, "Doctor")
        self.assertContains(response, 'name="password"')

    def test_get_changes_nothing(self):
        self.login(self.other_owner)
        self.client.get(self.join_url())
        self.assertTrue(self.reload_invite().is_pending)

    def test_accept_with_your_password(self):
        patient = self.make_patient()
        self.login(self.other_owner)
        response = self.accept()
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)

        invite = self.reload_invite()
        self.assertFalse(invite.is_pending)
        self.assertTrue(invite.is_active)
        self.assertEqual(invite.invite_token, "")
        self.assertIn(self.other_owner, self.clinic.doctors)
        # They are now working in the clinic they joined, and can open its records.
        self.assertEqual(self.client.session["clinic_id"], self.clinic.pk)
        self.assertEqual(self.client.get(reverse("patients:detail", args=[patient.pk])).status_code, 200)

        accepted = AuditLog.objects.get(action=Action.UPDATE, object_type="Membership", object_id=str(invite.pk))
        self.assertEqual(accepted.clinic, self.clinic)
        self.assertEqual(accepted.user, self.other_owner)
        self.assertEqual(accepted.summary, "Accepted the invitation to join as Doctor")
        # Moving clinics is in both audit logs.
        moves = AuditLog.objects.filter(user=self.other_owner, summary__icontains="switched")
        self.assertEqual(
            {(entry.action, entry.clinic) for entry in moves},
            {(Action.LOGOUT, self.other_clinic), (Action.LOGIN, self.clinic)},
        )

    def test_the_link_works_only_once(self):
        self.login(self.other_owner)
        self.accept()
        self.assertEqual(self.client.get(self.join_url(self.invite)).status_code, 404)

    def test_wrong_password_is_refused_and_counts_towards_the_lockout(self):
        self.login(self.other_owner)
        response = self.accept(password="not-my-password")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That isn&#x27;t your password")
        self.assertTrue(self.reload_invite().is_pending)

        with self.settings(LOGIN_MAX_ATTEMPTS=3):
            self.accept(password="not-my-password")
            self.accept(password="not-my-password")
            response = self.accept()  # right password, but now locked
        self.assertContains(response, "Too many failed attempts")
        self.assertTrue(self.reload_invite().is_pending)

    def test_someone_elses_link_is_404(self):
        """Even the inviting clinic's own owner, or a colleague, can't use it."""
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.get(self.join_url()).status_code, 404)
                self.assertEqual(self.accept().status_code, 404)
        self.assertTrue(self.reload_invite().is_pending)

    def test_knowing_the_password_without_the_link_is_not_enough(self):
        self.login(self.other_owner)
        response = self.accept(url=reverse("accounts:join", args=["made-up-token"]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.reload_invite().is_pending)

    def test_expired_link_is_refused(self):
        Membership.objects.filter(pk=self.invite.pk).update(invited_at=timezone.now() - timedelta(days=8))
        self.login(self.other_owner)
        response = self.client.get(self.join_url())
        self.assertContains(response, "This link has expired")
        self.assertNotContains(response, 'name="password"')

        response = self.accept()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.reload_invite().is_pending)

    def test_paused_clinic_link_is_404(self):
        self.clinic.is_active = False
        self.clinic.save()
        self.login(self.other_owner)
        self.assertEqual(self.client.get(self.join_url()).status_code, 404)

    def test_decline(self):
        self.login(self.other_owner)
        response = self.client.post(self.join_url(), {"action": "decline"})
        self.assertRedirects(response, DASHBOARD_URL, fetch_redirect_response=False)
        self.assertIsNone(self.reload_invite())
        self.assertTrue(
            AuditLog.objects.filter(
                action=Action.DELETE, clinic=self.clinic, summary="Declined the invitation to join"
            ).exists()
        )

    def test_refused_until_you_choose_your_own_password(self):
        """A password a clinic owner chose (and so knows) can't be used to join another clinic."""
        User.objects.filter(pk=self.other_owner.pk).update(must_change_password=True)
        self.login(self.other_owner)
        for response in (self.client.get(self.join_url()), self.accept()):
            self.assertRedirects(response, reverse("accounts:password_change"), fetch_redirect_response=False)
        self.assertTrue(self.reload_invite().is_pending)
