import codecs
import csv
import io
from datetime import date, timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Clinic, Membership
from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase, make_clinic, make_patient, make_user
from apps.patients import importer
from apps.patients.forms import years_before
from apps.patients.models import Patient

IMPORT_URL = reverse("patients:import")
TEMPLATE_URL = reverse("patients:import_template")

HEADER = "full_name,sex,date_of_birth,age,phone,whatsapp_phone,guardian_name,city,address,allergies,chronic_conditions,notes,mrn"


def csv_upload(text, name="patients.csv", bom=False, encoding="utf-8"):
    data = text.encode(encoding)
    if bom:
        data = codecs.BOM_UTF8 + data
    return SimpleUploadedFile(name, data, content_type="text/csv")


def rows(*lines):
    return "\r\n".join((HEADER,) + lines) + "\r\n"


class ImportAccessTests(ClinicTestCase):
    def test_owner_sees_the_how_to_page(self):
        self.login(self.owner)
        response = self.client.get(IMPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import patients from a spreadsheet")
        self.assertContains(response, "How it works")
        self.assertContains(response, TEMPLATE_URL)
        for column in importer.COLUMNS:
            self.assertContains(response, f"<code>{column}</code>")

    def test_doctor_and_receptionist_are_forbidden(self):
        for user in (self.doctor, self.receptionist):
            with self.subTest(user=user):
                self.login(user)
                self.assertEqual(self.client.get(IMPORT_URL).status_code, 403)
                upload = csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,"))
                self.assertEqual(self.client.post(IMPORT_URL, {"file": upload}).status_code, 403)
                self.assertEqual(self.client.get(TEMPLATE_URL).status_code, 403)
        self.assertFalse(Patient.objects.exists())

    def test_sign_in_required(self):
        response = self.client.get(IMPORT_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


class ImportTemplateTests(ClinicTestCase):
    def test_template_is_header_plus_one_example_row(self):
        self.login(self.owner)
        response = self.client.get(TEMPLATE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])

        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"), "starts with a BOM so Excel reads UTF-8")
        lines = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(lines[0], list(importer.COLUMNS))
        self.assertEqual(len(lines), 2)
        self.assertIn("0300-1234567", lines[1])

    def test_indian_clinic_gets_an_indian_example(self):
        clinic = make_clinic("Jaipur Clinic", country=Clinic.Country.INDIA, timezone="Asia/Kolkata")
        self.login(make_user(clinic, Membership.Role.OWNER))
        text = self.client.get(TEMPLATE_URL).content.decode("utf-8")
        self.assertIn("98765 43210", text)

    def test_template_file_imports_cleanly(self):
        """The example row in the template must itself pass the checks."""
        result = importer.parse_patients_csv(importer.template_csv("PK").encode("utf-8"), self.clinic)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(len(result.patients), 1)

    def test_post_not_allowed(self):
        self.login(self.owner)
        self.assertEqual(self.client.post(TEMPLATE_URL).status_code, 405)


class ImportTests(ClinicTestCase):
    def setUp(self):
        self.login(self.owner)

    def post(self, upload):
        return self.client.post(IMPORT_URL, {"file": upload})

    def test_happy_path_with_bom_and_messy_headers(self):
        text = (
            "Full Name,SEX,Date of Birth,Age,Phone,WhatsApp Phone,Guardian Name,City,Notes,Favourite colour\r\n"
            "Ayesha Khan,F,1988-04-15,,0300-1234567,,Imran Khan,Lahore,Prefers Urdu,blue\r\n"
            "Bilal  Ahmed,male,15/04/1990,,0321 7654321,0333-5556667,,,,\r\n"
            "Zainab Bibi,Female,,62,+92 345 1112223,,,,,\r\n"
        )
        response = self.post(csv_upload(text, bom=True))
        self.assertRedirects(response, f"{reverse('patients:list')}?sort=recent", fetch_redirect_response=False)
        self.assertEqual([str(m) for m in get_messages(response.wsgi_request)], ["Imported 3 patients."])

        ayesha = Patient.objects.get(full_name="Ayesha Khan")
        self.assertEqual(ayesha.clinic, self.clinic)
        self.assertEqual(ayesha.sex, Patient.Sex.FEMALE)
        self.assertEqual(ayesha.date_of_birth, date(1988, 4, 15))
        self.assertEqual(ayesha.guardian_name, "Imran Khan")
        self.assertEqual(ayesha.whatsapp_number, "923001234567")
        self.assertEqual(ayesha.created_by, self.owner)
        self.assertTrue(ayesha.mrn.startswith("P-"))

        bilal = Patient.objects.get(full_name="Bilal Ahmed")  # extra spaces tidied
        self.assertEqual(bilal.sex, Patient.Sex.MALE)
        self.assertEqual(bilal.date_of_birth, date(1990, 4, 15))
        self.assertEqual(bilal.whatsapp_number, "923335556667")

        zainab = Patient.objects.get(full_name="Zainab Bibi")
        self.assertEqual(zainab.date_of_birth, years_before(timezone.localdate(), 62))
        self.assertTrue(zainab.dob_is_estimated)

        self.assertEqual(len({p.mrn for p in Patient.objects.all()}), 3)
        log = AuditLog.objects.get(action=AuditLog.Action.IMPORT)
        self.assertEqual(log.summary, "Imported 3 patients from CSV")
        self.assertEqual(log.clinic, self.clinic)
        self.assertEqual(log.user, self.owner)

    def test_works_without_bom_and_keeps_clinical_columns(self):
        upload = csv_upload(rows("Ali Raza,O,,,0300-1234567,,,Multan,,Penicillin,Asthma,,"))
        self.assertEqual(self.post(upload).status_code, 302)
        patient = Patient.objects.get()
        self.assertEqual(patient.sex, Patient.Sex.OTHER)
        self.assertEqual(patient.allergies, "Penicillin")
        self.assertEqual(patient.chronic_conditions, "Asthma")
        self.assertEqual(patient.city, "Multan")
        self.assertIsNone(patient.date_of_birth)

    def test_any_bad_row_means_nothing_is_imported(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        upload = csv_upload(rows(
            "Good Patient,M,,,0300-1234567,,,,,,,,",  # row 2: fine
            ",F,,,0300-1234568,,,,,,,,",  # row 3: no name
            "Bad Sex,X,,,0300-1234569,,,,,,,,",  # row 4
            "Bad Date,M,31/02/1990,,0300-1234570,,,,,,,,",  # row 5
            "Bad Phone,M,,,12,,,,,,,,",  # row 6
            "Bad Age,F,,abc,0300-1234571,,,,,,,,",  # row 7
            f"Future Birthday,F,{tomorrow},,0300-1234572,,,,,,,,",  # row 8
            "Bad WhatsApp,F,,,0300-1234573,xyz,,,,,,,",  # row 9
            "No Phone,F,,,,,,,,,,,",  # row 10
        ))
        response = self.post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Patient.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.IMPORT).exists())
        self.assertContains(response, "Nothing was imported")

        problem_rows = [row for row, _ in response.context["errors"]]
        self.assertEqual(problem_rows, [3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(response.context["error_rows"], 8)
        for row in problem_rows:
            self.assertContains(response, f"Row {row}</td>")
        self.assertContains(response, "full_name is empty.")
        self.assertContains(response, "sex must be M, F or O")
        self.assertContains(response, "is not a date")
        self.assertContains(response, "e.g. 0300-1234567")
        self.assertContains(response, "date_of_birth is in the future.")
        self.assertContains(response, "phone is empty.")

    def test_mrn_must_be_unique_in_clinic_and_in_file(self):
        existing = self.make_patient(mrn="F-100")
        make_patient(self.other_clinic, mrn="F-200")  # another clinic's numbers don't matter
        upload = csv_upload(rows(
            "Ali Raza,M,,,0300-1234567,,,,,,,,F-100",  # row 2: taken in this clinic
            "Sara Khan,F,,,0300-1234568,,,,,,,,F-200",  # row 3: fine
            "Omar Shah,M,,,0300-1234569,,,,,,,,F-200",  # row 4: same as row 3
        ))
        response = self.post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["errors"],
            [
                (2, f"MR number {existing.mrn} is already used by another patient in this clinic."),
                (4, "MR number F-200 is also used in row 3."),
            ],
        )
        self.assertEqual(Patient.objects.filter(clinic=self.clinic).count(), 1)

    def test_own_mrns_are_kept_and_auto_numbers_skip_past_them(self):
        upload = csv_upload(rows(
            "Ali Raza,M,,,0300-1234567,,,,,,,,P-00050",
            "Sara Khan,F,,,0300-1234568,,,,,,,,",
        ))
        self.assertEqual(self.post(upload).status_code, 302)
        self.assertEqual(Patient.objects.get(full_name="Ali Raza").mrn, "P-00050")
        self.assertEqual(Patient.objects.get(full_name="Sara Khan").mrn, "P-00051")
        # Patients added later never clash with imported numbers.
        self.assertEqual(self.make_patient(full_name="Later Patient").mrn, "P-00052")

    def test_blank_lines_are_skipped_but_row_numbers_match_the_spreadsheet(self):
        upload = csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,", ",,,,,,,,,,,,", "", "Bad,Q,,,0300-1,,,,,,,,"))
        response = self.post(upload)
        self.assertEqual({row for row, _ in response.context["errors"]}, {5})

    def test_missing_required_columns(self):
        response = self.post(csv_upload("full_name,phone\r\nAli Raza,0300-1234567\r\n"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Missing: sex", response.context["form"].errors["file"][0])
        self.assertFalse(Patient.objects.exists())

    def test_file_must_be_utf8(self):
        response = self.post(csv_upload(rows("Zoë Khan,F,,,0300-1234567,,,,,,,,"), encoding="cp1252"))
        self.assertIn("not saved as UTF-8", response.context["form"].errors["file"][0])
        self.assertFalse(Patient.objects.exists())

    def test_file_must_be_csv(self):
        response = self.post(csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,"), name="patients.xlsx"))
        self.assertIn("file", response.context["form"].errors)

    def test_file_over_2_mb_is_refused(self):
        filler = "Ali Raza,M,,,0300-1234567,,,,,,,," + "x" * 200
        text = rows(*[filler] * 11000)
        self.assertGreater(len(text), 2 * 1024 * 1024)
        response = self.post(csv_upload(text))
        self.assertIn("bigger than 2 MB", response.context["form"].errors["file"][0])
        self.assertFalse(Patient.objects.exists())

    def test_more_than_5000_rows_is_refused(self):
        lines = [f"Patient {n},M,,,0300-{1000000 + n},,,,,,,," for n in range(importer.MAX_ROWS + 1)]
        response = self.post(csv_upload(rows(*lines)))
        self.assertIn("more than 5,000 patients", response.context["form"].errors["file"][0])
        self.assertFalse(Patient.objects.exists())

    def test_header_only_or_empty_file(self):
        for text in (HEADER + "\r\n", ""):
            with self.subTest(text=text[:10]):
                response = self.post(csv_upload(text))
                self.assertEqual(response.status_code, 200)
                self.assertIn("file", response.context["form"].errors)
        self.assertFalse(Patient.objects.exists())

    def test_clash_while_saving_shows_a_message_not_an_error_page(self):
        upload = csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,"))
        with mock.patch.object(importer, "save_imported_patients", side_effect=IntegrityError):
            response = self.post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing was imported", response.context["form"].errors["file"][0])
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.IMPORT).exists())

    def test_patients_go_into_the_owners_clinic_only(self):
        self.post(csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,")))
        self.assertEqual(Patient.objects.get().clinic, self.clinic)
        self.assertFalse(Patient.objects.filter(clinic=self.other_clinic).exists())
