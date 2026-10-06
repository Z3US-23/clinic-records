from django.contrib import admin
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.testing import FAST_PASSWORD_HASHERS, make_clinic, make_patient, make_user
from apps.patients.admin import PatientAdmin
from apps.patients.models import Patient


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class PatientAdminTests(TestCase):
    """The Django admin is for the platform operator (superuser), not clinic staff."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            email="operator@example.test", password="not-used-123", full_name="Platform Operator"
        )
        cls.clinic = make_clinic("Al-Noor Family Clinic")
        cls.patient = make_patient(cls.clinic, full_name="Ayesha Khan")
        cls.archived = make_patient(cls.clinic, full_name="Old Patient", is_archived=True, phone="0321-7654321")

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_registered_with_the_agreed_columns_and_filters(self):
        model_admin = admin.site._registry[Patient]
        self.assertIsInstance(model_admin, PatientAdmin)
        for column in ("full_name", "mrn", "clinic", "phone"):
            self.assertIn(column, model_admin.list_display)
        self.assertEqual(model_admin.list_filter, ("clinic", "is_archived"))
        self.assertIn("full_name", model_admin.search_fields)
        self.assertIn("mrn", model_admin.search_fields)

    def test_changelist_search_filter_and_change_page_render(self):
        url = reverse("admin:patients_patient_changelist")
        response = self.client.get(url)
        self.assertContains(response, "Ayesha Khan")
        self.assertContains(response, self.patient.mrn)

        response = self.client.get(url, {"q": "ayesha"})
        self.assertContains(response, "Ayesha Khan")
        self.assertNotContains(response, "Old Patient")

        response = self.client.get(url, {"is_archived__exact": "1"})
        self.assertContains(response, "Old Patient")
        self.assertNotContains(response, "Ayesha Khan")

        response = self.client.get(reverse("admin:patients_patient_change", args=[self.patient.pk]))
        self.assertEqual(response.status_code, 200)

    def test_clinic_staff_cannot_use_the_admin(self):
        owner = make_user(self.clinic, Membership.Role.OWNER)
        self.client.force_login(owner)
        response = self.client.get(reverse("admin:patients_patient_changelist"))
        self.assertEqual(response.status_code, 302)
