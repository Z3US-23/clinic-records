from django.contrib.admin.sites import site
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.admin import AuditLogAdmin
from apps.core.models import AuditLog

from .base import CoreTestCase

AUDIT_LOG = reverse("core:audit_log")


class AuditLogAccessTests(CoreTestCase):
    def test_owner_only(self):
        self.login(self.owner)
        response = self.client.get(AUDIT_LOG)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/audit_log.html")
        for user in (self.doctor, self.receptionist):
            self.login(user)
            self.assertEqual(self.client.get(AUDIT_LOG).status_code, 403)

    def test_sign_in_required(self):
        response = self.client.get(AUDIT_LOG)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_post_not_allowed(self):
        self.login(self.owner)
        self.assertEqual(self.client.post(AUDIT_LOG).status_code, 405)

    def test_explains_what_is_recorded(self):
        self.login(self.owner)
        self.assertContains(self.client.get(AUDIT_LOG), "Medical records are private")


class AuditLogContentTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        # Signing in writes its own "Signed in" entry; start each test with an empty log.
        self.login(self.owner)
        AuditLog.objects.all().delete()

    def entry(self, clinic=None, user=None, action=AuditLog.Action.VIEW, days_ago=0, **kwargs):
        entry = AuditLog.objects.create(
            clinic=clinic or self.clinic, user=user or self.doctor, action=action,
            object_type=kwargs.pop("object_type", "Patient"), object_id=kwargs.pop("object_id", "7"),
            summary=kwargs.pop("summary", "Viewed patient P-00007"), ip_address=kwargs.pop("ip_address", "10.0.0.5"),
        )
        if days_ago:
            AuditLog.objects.filter(pk=entry.pk).update(created_at=self.at(self.days_from_today(-days_ago), 12))
        return entry

    def entries_shown(self, **params):
        response = self.client.get(AUDIT_LOG, params)
        self.assertEqual(response.status_code, 200)
        return list(response.context["page_obj"].object_list)

    def test_only_this_clinics_entries(self):
        mine = self.entry(summary="Viewed patient P-00001")
        self.entry(clinic=self.other_clinic, user=self.other_owner, summary="OTHER-CLINIC-ENTRY")
        AuditLog.objects.create(clinic=None, action=AuditLog.Action.LOGIN_FAILED, summary="NO-CLINIC-ENTRY")
        response = self.client.get(AUDIT_LOG)
        self.assertEqual(list(response.context["page_obj"].object_list), [mine])
        self.assertNotContains(response, "OTHER-CLINIC-ENTRY")
        self.assertNotContains(response, "NO-CLINIC-ENTRY")

    def test_table_columns(self):
        self.entry(action=AuditLog.Action.UPDATE, object_type="LabResult", object_id="42", summary="Updated lab result")
        response = self.client.get(AUDIT_LOG)
        self.assertContains(response, "Bilal Hussain")
        self.assertContains(response, '<span class="badge badge-primary">Updated</span>', html=True)
        self.assertContains(response, "Lab result #42")
        self.assertContains(response, "Updated lab result")
        self.assertContains(response, "10.0.0.5")
        self.assertContains(response, 'data-label="Staff member"')  # stacks nicely on phones

    def test_filter_by_user_action_and_dates(self):
        doctor_view = self.entry(user=self.doctor, action=AuditLog.Action.VIEW, days_ago=1)
        receptionist_update = self.entry(user=self.receptionist, action=AuditLog.Action.UPDATE, days_ago=1)
        old_view = self.entry(user=self.doctor, action=AuditLog.Action.VIEW, days_ago=10)

        self.assertEqual(self.entries_shown(user=self.receptionist.pk), [receptionist_update])
        self.assertEqual(set(self.entries_shown(action="view")), {doctor_view, old_view})
        yesterday = self.days_from_today(-1).isoformat()
        self.assertEqual(set(self.entries_shown(date_from=yesterday)), {doctor_view, receptionist_update})
        self.assertEqual(self.entries_shown(date_to=self.days_from_today(-5).isoformat()), [old_view])
        self.assertEqual(
            self.entries_shown(user=self.doctor.pk, action="view", date_from=yesterday, date_to=yesterday), [doctor_view]
        )

    def test_invalid_filters_are_ignored(self):
        entries = {self.entry(), self.entry(user=self.receptionist)}
        self.assertEqual(set(self.entries_shown(date_from="not-a-date", date_to="2026-13-45")), entries)
        self.assertEqual(set(self.entries_shown(action="hack")), entries)
        # Another clinic's staff member can't be used to filter (and isn't offered).
        self.assertEqual(set(self.entries_shown(user=self.other_owner.pk)), entries)
        self.assertEqual(set(self.entries_shown(user="abc")), entries)

    def test_user_choices_are_this_clinics_staff(self):
        choices = self.client.get(AUDIT_LOG).context["form"].fields["user"].queryset
        self.assertEqual(set(choices), {self.owner, self.doctor, self.receptionist})
        self.assertNotIn(self.other_owner, choices)

    def test_waiting_invitation_is_not_a_choice(self):
        """Until they accept, the clinic only knows the email it typed, not the account's name."""
        Membership(user=self.other_owner, clinic=self.clinic, role=Membership.Role.DOCTOR).start_invitation()
        response = self.client.get(AUDIT_LOG)
        self.assertNotIn(self.other_owner, response.context["form"].fields["user"].queryset)
        self.assertNotContains(response, self.other_owner.full_name)

    def test_paginated_by_50(self):
        AuditLog.objects.bulk_create(
            AuditLog(clinic=self.clinic, user=self.doctor, action=AuditLog.Action.VIEW, summary=f"Entry {n}")
            for n in range(55)
        )
        first = self.client.get(AUDIT_LOG)
        self.assertEqual(len(first.context["page_obj"].object_list), 50)
        second = self.client.get(AUDIT_LOG, {"page": 2})
        self.assertEqual(len(second.context["page_obj"].object_list), 5)

    def test_newest_first(self):
        old = self.entry(days_ago=3)
        new = self.entry()
        self.assertEqual(self.entries_shown(), [new, old])


class AuditLogAdminTests(CoreTestCase):
    """The audit trail can be read in the Django admin but never added, changed or deleted."""

    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser(
            email="platform@example.test", password="admin-pass-12345", full_name="Platform Admin"
        )
        self.entry = AuditLog.objects.create(clinic=self.clinic, user=self.doctor, action=AuditLog.Action.VIEW)

    def test_model_admin_is_read_only(self):
        model_admin = AuditLogAdmin(AuditLog, site)
        request = RequestFactory().get("/admin/")
        request.user = self.admin_user
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.entry))
        self.assertFalse(model_admin.has_delete_permission(request, self.entry))
        self.assertTrue(model_admin.has_view_permission(request, self.entry))

    def test_admin_pages(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(reverse("admin:core_auditlog_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_auditlog_change", args=[self.entry.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_auditlog_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:core_auditlog_delete", args=[self.entry.pk])).status_code, 403)
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())
