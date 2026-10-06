"""Lab results: adding (with file checks), opening the private file, deleting."""

import os
from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.clinical.models import LabResult
from apps.core.audit import Action
from apps.core.testing import make_patient

from .base import HTML_BYTES, JPEG_BYTES, PDF_BYTES, PNG_BYTES, ClinicalTestCase, TempMediaMixin, upload


def create_url(patient, visit=None):
    url = reverse("clinical:lab_create", args=[patient.pk])
    return f"{url}?visit={visit.pk}" if visit else url


def file_url(lab):
    return reverse("clinical:lab_file", args=[lab.pk])


def delete_url(lab):
    return reverse("clinical:lab_delete", args=[lab.pk])


def lab_post_data(**fields):
    data = {
        "test_name": "HbA1c",
        "result_date": timezone.localdate().isoformat(),
        "result_text": "7.8 %",
        "visit": "",
    }
    data.update(fields)
    return data


def read_and_close(response):
    """Body of a file response. Consuming it also closes the file (Windows can't delete open files)."""
    return b"".join(response.streaming_content)


class LabCreateTests(TempMediaMixin, ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)

    def labs_tab_url(self):
        return reverse("patients:detail", args=[self.patient.pk]) + "?tab=labs"

    def test_clinicians_see_the_form(self):
        visit = self.make_visit(self.patient)
        self.make_visit(make_patient(self.clinic, full_name="Usman Tariq"))  # someone else's visit
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(create_url(self.patient))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "clinical/lab_form.html")
                self.assertContains(response, 'enctype="multipart/form-data"')
                self.assertContains(response, "Allergies: Penicillin")
                visits = list(response.context["form"].fields["visit"].queryset)
                self.assertEqual(visits, [visit])  # only this patient's visits

    def test_visit_is_preselected_from_the_link(self):
        visit = self.make_visit(self.patient)
        response = self.client.get(create_url(self.patient, visit))
        self.assertEqual(response.context["form"].initial["visit"], visit)
        self.assertContains(response, visit.get_absolute_url())  # Cancel goes back to the visit

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(create_url(self.patient)).status_code, 403)
        response = self.client.post(create_url(self.patient), lab_post_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LabResult.objects.exists())

    def test_other_clinics_patient_is_not_found(self):
        self.assertEqual(self.client.get(create_url(self.other_patient)).status_code, 404)
        self.assertEqual(self.client.post(create_url(self.other_patient), lab_post_data()).status_code, 404)
        self.assertFalse(LabResult.objects.exists())

    def test_add_result_with_pdf(self):
        data = lab_post_data(is_abnormal="on", file=upload("HbA1c Oct.pdf", PDF_BYTES))
        response = self.client.post(create_url(self.patient), data)
        self.assertRedirects(response, self.labs_tab_url(), fetch_redirect_response=False)

        lab = LabResult.objects.get()
        self.assertEqual(lab.clinic, self.clinic)
        self.assertEqual(lab.patient, self.patient)
        self.assertEqual(lab.uploaded_by, self.doctor)
        self.assertTrue(lab.is_abnormal)
        self.assertEqual(lab.original_filename, "HbA1c Oct.pdf")
        # Stored under a random name: no patient details in the storage path.
        self.assertRegex(lab.file.name, rf"^clinic_{self.clinic.pk}/labs/[0-9a-f]{{32}}\.pdf$")
        self.assertTrue(lab.file.path.startswith(str(self.media_root)))
        with lab.file.open("rb") as stored:
            self.assertEqual(stored.read(), PDF_BYTES)
        self.assertTrue(self.audit_exists(Action.CREATE, lab, f"Added lab result for {self.patient.mrn}"))

    def test_add_result_without_a_file(self):
        response = self.client.post(create_url(self.patient), lab_post_data())
        self.assertEqual(response.status_code, 302)
        lab = LabResult.objects.get()
        self.assertFalse(lab.file)
        self.assertEqual(lab.original_filename, "")

    def test_photos_are_accepted(self):
        for name, content in [("scan.png", PNG_BYTES), ("scan.JPG", JPEG_BYTES), ("scan.jpeg", JPEG_BYTES)]:
            with self.subTest(name=name):
                response = self.client.post(create_url(self.patient), lab_post_data(file=upload(name, content)))
                self.assertEqual(response.status_code, 302)
        self.assertEqual(LabResult.objects.count(), 3)

    def test_created_from_a_visit_goes_back_to_the_visit(self):
        visit = self.make_visit(self.patient)
        response = self.client.post(create_url(self.patient, visit), lab_post_data(visit=str(visit.pk)))
        self.assertRedirects(response, visit.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(LabResult.objects.get().visit, visit)

    def test_renamed_file_is_rejected(self):
        for name, content in [("report.pdf", HTML_BYTES), ("report.png", PDF_BYTES), ("report.jpg", PNG_BYTES)]:
            with self.subTest(name=name):
                response = self.client.post(create_url(self.patient), lab_post_data(file=upload(name, content)))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].has_error("file", "signature"))
        self.assertFalse(LabResult.objects.exists())

    def test_other_file_types_are_rejected(self):
        for name in ("report.html", "report.exe", "report.svg", "report"):
            with self.subTest(name=name):
                response = self.client.post(create_url(self.patient), lab_post_data(file=upload(name, PDF_BYTES)))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].has_error("file"))
        self.assertFalse(LabResult.objects.exists())

    @override_settings(LAB_UPLOAD_MAX_MB=1)
    def test_large_file_is_rejected(self):
        too_big = PDF_BYTES + b"0" * (1024 * 1024)
        response = self.client.post(create_url(self.patient), lab_post_data(file=upload("big.pdf", too_big)))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("file", "too_large"))
        self.assertContains(response, "The limit is 1 MB")
        self.assertFalse(LabResult.objects.exists())

    def test_visit_of_another_patient_or_clinic_is_rejected(self):
        someone_else = make_patient(self.clinic, full_name="Usman Tariq")
        their_visit = self.make_visit(someone_else)
        other_clinic_visit = self.make_visit(self.other_patient, doctor=self.other_owner)
        for visit in (their_visit, other_clinic_visit):
            with self.subTest(visit=visit.pk):
                response = self.client.post(create_url(self.patient), lab_post_data(visit=str(visit.pk)))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].has_error("visit"))
        self.assertFalse(LabResult.objects.exists())

    def test_result_date_cannot_be_in_the_future(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.post(create_url(self.patient), lab_post_data(result_date=tomorrow))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("result_date"))


class LabFileTests(TempMediaMixin, ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)

    def test_opens_inline_with_safe_headers(self):
        lab = self.make_lab(name="HbA1c report.pdf")
        response = self.client.get(file_url(lab))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_and_close(response), PDF_BYTES)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'inline; filename="HbA1c report.pdf"')
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        self.assertTrue(self.audit_exists(Action.VIEW, lab, f"Opened lab report for {self.patient.mrn}"))

    def test_photo_content_types(self):
        for name, content, content_type in [
            ("scan.png", PNG_BYTES, "image/png"),
            ("scan.jpg", JPEG_BYTES, "image/jpeg"),
        ]:
            with self.subTest(name=name):
                lab = self.make_lab(content=content, name=name)
                response = self.client.get(file_url(lab))
                read_and_close(response)
                self.assertEqual(response["Content-Type"], content_type)

    def test_owner_can_open(self):
        lab = self.make_lab()
        self.login(self.owner)
        response = self.client.get(file_url(lab))
        self.assertEqual(response.status_code, 200)
        read_and_close(response)

    def test_other_clinics_file_is_not_found(self):
        lab = self.make_lab(patient=self.other_patient)
        self.assertEqual(self.client.get(file_url(lab)).status_code, 404)

    def test_result_without_a_file_is_not_found(self):
        lab = self.make_lab(content=None)
        self.assertEqual(self.client.get(file_url(lab)).status_code, 404)

    def test_file_missing_from_storage_is_not_found(self):
        lab = self.make_lab()
        lab.file.storage.delete(lab.file.name)
        with self.assertLogs("apps.clinical.views", level="WARNING"):
            self.assertEqual(self.client.get(file_url(lab)).status_code, 404)

    def test_receptionist_is_forbidden(self):
        lab = self.make_lab()
        self.login(self.receptionist)
        self.assertEqual(self.client.get(file_url(lab)).status_code, 403)

    def test_signed_out_user_goes_to_login(self):
        lab = self.make_lab()
        self.client.logout()
        response = self.client.get(file_url(lab))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_no_public_media_url(self):
        self.assertFalse(settings.MEDIA_URL.startswith("/media"))
        lab = self.make_lab()
        response = self.client.get(settings.MEDIA_URL + lab.file.name)
        self.assertEqual(response.status_code, 404)


class LabDeleteTests(TempMediaMixin, ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)
        self.lab = self.make_lab()
        self.stored_path = self.lab.file.path

    def test_get_is_not_allowed(self):
        response = self.client.get(delete_url(self.lab))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(LabResult.objects.filter(pk=self.lab.pk).exists())

    def test_delete_removes_the_result_and_its_file(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(delete_url(self.lab))
        expected = reverse("patients:detail", args=[self.patient.pk]) + "?tab=labs"
        self.assertRedirects(response, expected, fetch_redirect_response=False)
        self.assertFalse(LabResult.objects.filter(pk=self.lab.pk).exists())
        self.assertFalse(os.path.exists(self.stored_path))
        self.assertTrue(self.audit_exists(Action.DELETE, self.lab, f"Deleted lab result for {self.patient.mrn}"))

    def test_result_without_a_file_can_be_deleted(self):
        lab = self.make_lab(content=None)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(delete_url(lab))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LabResult.objects.filter(pk=lab.pk).exists())

    def test_other_clinics_result_is_not_found(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.post(delete_url(self.lab)).status_code, 404)
        self.assertTrue(LabResult.objects.filter(pk=self.lab.pk).exists())
        self.assertTrue(os.path.exists(self.stored_path))

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.post(delete_url(self.lab)).status_code, 403)
        self.assertTrue(LabResult.objects.filter(pk=self.lab.pk).exists())
