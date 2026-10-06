import csv
import io
import shutil
import tempfile
import zipfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.clinical.models import LabResult, PrescriptionItem
from apps.core.exports import safe_cell
from apps.core.models import AuditLog

from .base import KARACHI, CoreTestCase

EXPORT = reverse("core:export")
CSV_FILES = [
    "patients.csv", "visits.csv", "prescriptions.csv", "lab_results.csv",
    "appointments.csv", "reminders.csv", "staff.csv",
]


class SafeCellTests(CoreTestCase):
    def test_formula_like_text_is_neutralised(self):
        for dangerous in ("=1+1", "+92 300 1234567", "-2+3", "@SUM(A1)", "\tTabbed", "\rReturn"):
            with self.subTest(value=dangerous):
                self.assertEqual(safe_cell(dangerous), "'" + dangerous)

    def test_ordinary_values(self):
        self.assertEqual(safe_cell("Ayesha Khan"), "Ayesha Khan")
        self.assertEqual(safe_cell(None), "")
        self.assertEqual(safe_cell(True), "yes")
        self.assertEqual(safe_cell(False), "no")
        self.assertEqual(safe_cell(42), "42")
        self.assertEqual(safe_cell(date(2026, 10, 6)), "2026-10-06")


class ExportPageTests(CoreTestCase):
    def test_owner_only(self):
        self.login(self.owner)
        self.assertEqual(self.client.get(EXPORT).status_code, 200)
        for user in (self.doctor, self.receptionist):
            self.login(user)
            self.assertEqual(self.client.get(EXPORT).status_code, 403)
            self.assertEqual(self.client.post(EXPORT).status_code, 403)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.EXPORT).exists())

    def test_sign_in_required(self):
        response = self.client.post(EXPORT)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_get_shows_the_page_and_never_downloads(self):
        self.make_patient()
        self.login(self.owner)
        response = self.client.get(EXPORT)
        self.assertTemplateUsed(response, "core/export.html")
        self.assertTrue(response["Content-Type"].startswith("text/html"))
        self.assertFalse(response.has_header("Content-Disposition"))
        self.assertContains(response, "once a week")
        self.assertEqual(response.context["counts"]["patients"], 1)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.EXPORT).exists())

    def test_other_methods_not_allowed(self):
        self.login(self.owner)
        self.assertEqual(self.client.put(EXPORT).status_code, 405)
        self.assertEqual(self.client.delete(EXPORT).status_code, 405)


class ExportDownloadTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.patient = self.make_patient(full_name="Ayesha Khan", allergies="Penicillin")
        self.visit = self.make_visit(
            self.patient, diagnosis="Hypertension", bp_systolic=150, bp_diastolic=95,
            follow_up_date=self.days_from_today(30), plan="Less salt",
        )
        PrescriptionItem.objects.create(visit=self.visit, medicine="Tab. Amlodipine 5mg", frequency="0+0+1")
        LabResult.objects.create(clinic=self.clinic, patient=self.patient, visit=self.visit, test_name="HbA1c", result_text="8.4%", is_abnormal=True)
        self.appointment = self.make_appointment(self.patient)

        # Another clinic's records must never appear in this clinic's export.
        stranger = self.make_patient(self.other_clinic, full_name="OTHER-CLINIC-PATIENT")
        other_visit = self.make_visit(stranger, doctor=self.other_owner, diagnosis="OTHER-CLINIC-DIAGNOSIS")
        PrescriptionItem.objects.create(visit=other_visit, medicine="OTHER-CLINIC-MEDICINE")
        LabResult.objects.create(clinic=self.other_clinic, patient=stranger, test_name="OTHER-CLINIC-LAB")
        self.make_appointment(stranger, doctor=self.other_owner, reason="OTHER-CLINIC-REASON")
        self.make_reminder(stranger, message="OTHER-CLINIC-MESSAGE")

    def download(self, **data):
        self.login(self.owner)
        response = self.client.post(EXPORT, data)
        self.assertEqual(response.status_code, 200)
        content = b"".join(response.streaming_content)
        response.close()
        return response, zipfile.ZipFile(io.BytesIO(content))

    def read_csv(self, archive, name):
        raw = archive.read(name)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{name} should start with a UTF-8 BOM for Excel")
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))

    def test_zip_download(self):
        response, archive = self.download()
        today = timezone.localtime(timezone.now(), KARACHI)
        filename = f"{self.clinic.slug}-export-{today:%Y%m%d}.zip"
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{filename}"')
        self.assertIn("no-store", response["Cache-Control"])  # patient data: never cached
        self.assertEqual(sorted(archive.namelist()), sorted(CSV_FILES + ["README.txt"]))

    def test_contents(self):
        _, archive = self.download()
        patients = self.read_csv(archive, "patients.csv")
        self.assertEqual([p["full_name"] for p in patients], ["Ayesha Khan"])
        self.assertEqual(patients[0]["allergies"], "Penicillin")
        self.assertEqual(patients[0]["mr_number"], self.patient.mrn)

        visits = self.read_csv(archive, "visits.csv")
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["diagnosis"], "Hypertension")
        self.assertEqual(visits[0]["bp_systolic"], "150")
        self.assertEqual(visits[0]["follow_up_date"], self.days_from_today(30).isoformat())
        self.assertEqual(visits[0]["doctor"], "Dr. Bilal Hussain")

        self.assertEqual(self.read_csv(archive, "prescriptions.csv")[0]["medicine"], "Tab. Amlodipine 5mg")
        lab = self.read_csv(archive, "lab_results.csv")[0]
        self.assertEqual((lab["test_name"], lab["result"], lab["abnormal"]), ("HbA1c", "8.4%", "yes"))
        self.assertEqual(len(self.read_csv(archive, "appointments.csv")), 1)

        staff = self.read_csv(archive, "staff.csv")
        self.assertEqual({s["email"] for s in staff}, {"owner@example.test", "doctor@example.test", "reception@example.test"})
        self.assertEqual(set(staff[0]), {"name", "title", "email", "role", "qualifications", "registration_number", "active", "added_on"})

        readme = archive.read("README.txt").decode("utf-8")
        self.assertIn(self.clinic.name, readme)
        self.assertIn("patients.csv", readme)
        self.assertRegex(readme, r"visits\.csv\s+1 rows")
        self.assertIn("Lab report files included: no", readme)

    def test_only_this_clinics_data(self):
        _, archive = self.download()
        everything = b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8-sig")
        for marker in ("OTHER-CLINIC-PATIENT", "OTHER-CLINIC-DIAGNOSIS", "OTHER-CLINIC-MEDICINE",
                       "OTHER-CLINIC-LAB", "OTHER-CLINIC-REASON", "OTHER-CLINIC-MESSAGE", "other@example.test"):
            self.assertNotIn(marker, everything)

    def test_no_secrets(self):
        _, archive = self.download()
        everything = b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8-sig")
        self.assertNotIn(self.appointment.confirm_token, everything)
        self.assertNotIn(self.owner.password, everything)
        self.assertNotIn("md5$", everything)
        self.assertNotIn("pbkdf2", everything)

    def test_formula_injection_is_escaped(self):
        self.make_patient(
            full_name="=HYPERLINK(\"http://evil.test\",\"Click\")", guardian_name="+SUM(1,2)",
            address="@cmd", notes="-1+2", phone="0300-7654321",
        )
        _, archive = self.download()
        row = next(p for p in self.read_csv(archive, "patients.csv") if "HYPERLINK" in p["full_name"])
        self.assertEqual(row["full_name"], "'=HYPERLINK(\"http://evil.test\",\"Click\")")
        self.assertEqual(row["father_or_husband_name"], "'+SUM(1,2)")
        self.assertEqual(row["address"], "'@cmd")
        self.assertEqual(row["front_desk_notes"], "'-1+2")
        self.assertEqual(row["phone"], "0300-7654321")

    def test_urdu_and_hindi_names_survive(self):
        self.make_patient(full_name="علی رضا", phone="0300-1112223")
        self.make_patient(full_name="राहुल शर्मा", phone="0300-1112224")
        _, archive = self.download()
        names = {p["full_name"] for p in self.read_csv(archive, "patients.csv")}
        self.assertIn("علی رضا", names)
        self.assertIn("राहुल शर्मा", names)

    def test_export_is_audited(self):
        self.download()
        entry = AuditLog.objects.get(action=AuditLog.Action.EXPORT)
        self.assertEqual((entry.clinic, entry.user, entry.object_type), (self.clinic, self.owner, "Clinic"))


class ExportLabFilesTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.settings_override = override_settings(MEDIA_ROOT=self.media)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

        patient = self.make_patient()
        self.lab = LabResult.objects.create(
            clinic=self.clinic, patient=patient, test_name="CBC",
            file=SimpleUploadedFile("cbc report.pdf", b"%PDF-1.4 our report"), original_filename="cbc report.pdf",
        )
        stranger = self.make_patient(self.other_clinic)
        LabResult.objects.create(
            clinic=self.other_clinic, patient=stranger, test_name="Other",
            file=SimpleUploadedFile("other.pdf", b"%PDF-1.4 OTHER CLINIC"), original_filename="other.pdf",
        )

    def download(self, **data):
        self.login(self.owner)
        response = self.client.post(EXPORT, data)
        content = b"".join(response.streaming_content)
        response.close()
        return zipfile.ZipFile(io.BytesIO(content))

    def test_lab_files_only_when_asked(self):
        archive = self.download()
        self.assertFalse([name for name in archive.namelist() if name.startswith("labs/")])

    def test_lab_files_included(self):
        archive = self.download(include_lab_files="on")
        lab_files = [name for name in archive.namelist() if name.startswith("labs/")]
        self.assertEqual(lab_files, [f"labs/{self.lab.pk}-cbc_report.pdf"])
        self.assertEqual(archive.read(lab_files[0]), b"%PDF-1.4 our report")
        lab_row = archive.read("lab_results.csv").decode("utf-8-sig")
        self.assertIn(f"labs/{self.lab.pk}-cbc_report.pdf", lab_row)
        self.assertIn("Lab report files included: yes (1 files", archive.read("README.txt").decode())
        self.assertIn("with lab files", AuditLog.objects.get(action=AuditLog.Action.EXPORT).summary)

    def test_missing_file_on_disk_does_not_break_the_export(self):
        self.lab.file.storage.delete(self.lab.file.name)
        archive = self.download(include_lab_files="on")
        self.assertFalse([name for name in archive.namelist() if name.startswith("labs/")])
        self.assertIn("could not be found", archive.read("README.txt").decode())
