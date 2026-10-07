"""Shared set-up for the clinical tests."""

import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils.html import strip_tags

from apps.accounts.models import Membership
from apps.clinical.models import LabResult, PrescriptionItem
from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase, make_patient, make_user
from apps.patients.models import Patient

# Smallest byte strings that start like the real thing.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
HTML_BYTES = b"<html><script>alert('x')</script></html>"


def upload(name="report.pdf", content=PDF_BYTES, content_type="application/pdf"):
    return SimpleUploadedFile(name, content, content_type=content_type)


def visit_post_data(rows=(), initial_forms=0, **fields):
    """POST data for the visit form: visit fields + the prescription formset ("items-...")."""
    data = {
        "chief_complaint": "Cough for 2 weeks",
        "history": "",
        "examination": "",
        "diagnosis": "",
        "plan": "",
        "follow_up_date": "",
        "items-TOTAL_FORMS": str(len(rows)),
        "items-INITIAL_FORMS": str(initial_forms),
        "items-MIN_NUM_FORMS": "0",
        "items-MAX_NUM_FORMS": "40",
    }
    for index, row in enumerate(rows):
        for key, value in row.items():
            data[f"items-{index}-{key}"] = value
    data.update(fields)
    return data


class ClinicalTestCase(ClinicTestCase):
    """ClinicTestCase plus a patient in each clinic and a second doctor in the main clinic."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_doctor = make_user(
            cls.clinic, Membership.Role.DOCTOR, email="doctor2@example.test", full_name="Kamran Ali"
        )
        cls.patient = make_patient(
            cls.clinic, full_name="Ayesha Khan", sex=Patient.Sex.FEMALE, allergies="Penicillin"
        )
        cls.other_patient = make_patient(cls.other_clinic, full_name="Zara Other")

    def add_items(self, visit, *medicines):
        return [
            PrescriptionItem.objects.create(visit=visit, medicine=name, dose="1 tablet", frequency="1+0+1", order=i)
            for i, name in enumerate(medicines)
        ]

    def assert_allergy_banner(self, response, text):
        """The red allergy banner (includes/patient_banner.html) shows `text`, whatever markup it uses inside."""
        html = response.content.decode()
        self.assertIn('class="allergy-banner"', html)
        start = html.index('class="allergy-banner"')
        banner = html[html.index(">", start) + 1 : html.index("</div>", start)]
        self.assertIn(text, " ".join(strip_tags(banner).split()))

    def audit_exists(self, action, obj, summary, user=None):
        return AuditLog.objects.filter(
            action=action,
            object_type=obj.__class__.__name__,
            object_id=str(obj.pk),
            summary=summary,
            user=user or self.doctor,
        ).exists()


class TempMediaMixin:
    """Uploads go to a throwaway folder for the test class, deleted afterwards."""

    @classmethod
    def setUpClass(cls):
        cls.media_root = tempfile.mkdtemp(prefix="clinic-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls.media_root)
        cls._media_override.enable()
        # Class cleanups run last-in-first-out: restore settings, then remove the folder.
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.addClassCleanup(cls._media_override.disable)
        super().setUpClass()

    def make_lab(self, patient=None, content=PDF_BYTES, name="report.pdf", **kwargs):
        patient = patient or self.patient
        kwargs.setdefault("test_name", "HbA1c")
        if content is not None:
            kwargs["file"] = upload(name, content)
            kwargs.setdefault("original_filename", name)
        return LabResult.objects.create(clinic=patient.clinic, patient=patient, **kwargs)
