import codecs
import csv
import io
from datetime import date, timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext
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
            "No Phone,F,,,,,,,,,,,",  # row 10: fine, the mobile number is optional
        ))
        response = self.post(upload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Patient.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.IMPORT).exists())
        self.assertContains(response, "Nothing was imported")

        problem_rows = [row for row, _ in response.context["errors"]]
        self.assertEqual(problem_rows, [3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(response.context["error_rows"], 7)
        for row in problem_rows:
            self.assertContains(response, f"Row {row}</td>")
        self.assertContains(response, "full_name is empty.")
        self.assertContains(response, "sex must be M, F or O")
        self.assertContains(response, "is not a date")
        self.assertContains(response, "e.g. 0300-1234567")
        self.assertContains(response, "date_of_birth is in the future.")

    def test_mobile_number_is_optional_but_must_be_real_if_given(self):
        response = self.post(csv_upload(rows("No Phone,F,,,,,,,,,,,", "Bad Phone,M,,,0300-12,,,,,,,,")))
        self.assertEqual(response.context["errors"], [(3, "phone “0300-12” is not a mobile number (e.g. 0300-1234567).")])
        self.assertFalse(Patient.objects.exists())

        self.assertEqual(self.post(csv_upload(rows("No Phone,F,,,,,,,,,,,"))).status_code, 302)
        patient = Patient.objects.get()
        self.assertEqual(patient.phone, "")
        self.assertEqual(patient.whatsapp_number, "")
        self.assertFalse(patient.can_receive_whatsapp)
        self.assertNotIn("phone", importer.REQUIRED_COLUMNS)

    def test_landlines_and_short_numbers_are_not_mobiles(self):
        response = self.post(csv_upload(rows("Landline,F,,,042-35761234,,,,,,,,", "Short,M,,,0300-123456,,,,,,,,")))
        self.assertEqual([row for row, _ in response.context["errors"]], [2, 3])  # row 1 is the header
        self.assertFalse(Patient.objects.exists())

    def test_urdu_digits_are_saved_as_0_to_9(self):
        self.assertEqual(self.post(csv_upload(rows("Ali Raza,M,,,۰۳۰۰-۱۲۳۴۵۶۷,,,,,,,,"))).status_code, 302)
        patient = Patient.objects.get()
        self.assertEqual((patient.phone, patient.whatsapp_number), ("0300-1234567", "923001234567"))

    def test_age_must_be_a_plain_whole_number(self):
        """Regression: '²' passed isdigit() and then crashed int() with a server error."""
        for age in ("²", "①", "4.5", "-3", "121", "0" * 5000):
            with self.subTest(age=age[:10]):
                response = self.post(csv_upload(rows(f"Ali Raza,M,,{age},0300-1234567,,,,,,,,")))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "age must be a whole number from 0 to 120")
        self.assertFalse(Patient.objects.exists())

    def test_age_in_arabic_indic_digits_works(self):
        self.assertEqual(self.post(csv_upload(rows("Ali Raza,M,,٤٢,0300-1234567,,,,,,,,"))).status_code, 302)
        patient = Patient.objects.get()
        self.assertEqual(patient.date_of_birth, years_before(timezone.localdate(), 42))
        self.assertTrue(patient.dob_is_estimated)

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

    def test_auto_mrns_continue_from_the_counter_and_skip_numbers_in_use(self):
        self.make_patient(full_name="First Patient")  # P-00001 from the clinic's counter
        self.make_patient(full_name="Hand Numbered", phone="0300-7777777", mrn="P-00003")  # counter stays at 1
        upload = csv_upload(rows(
            "Ali Raza,M,,,0300-1234567,,,,,,,,",
            "Sara Khan,F,,,,0333 5556667,,,,,,,F-77",  # own, non-automatic number: kept, doesn't move the counter
            "Omar Shah,M,,,0300-1234569,,,,,,,,",
        ))
        self.assertEqual(self.post(upload).status_code, 302)
        mrns = dict(Patient.objects.values_list("full_name", "mrn"))
        self.assertEqual(mrns["Ali Raza"], "P-00004")
        self.assertEqual(mrns["Sara Khan"], "F-77")
        self.assertEqual(mrns["Omar Shah"], "P-00005")

        clinic = Clinic.objects.get(pk=self.clinic.pk)
        self.assertEqual(clinic.patient_counter, 5)
        self.assertEqual(clinic.allocate_mrn(), "P-00006")

    def test_imported_patients_get_their_whatsapp_number_and_timestamps(self):
        """The import saves in bulk (no Patient.save()), so it must still fill these in."""
        upload = csv_upload(rows("Ali Raza,M,,,0300-1234567,,,,,,,,", "Sara Khan,F,,,0300-1234568,0333 5556667,,,,,,,"))
        self.assertEqual(self.post(upload).status_code, 302)
        numbers = dict(Patient.objects.values_list("full_name", "whatsapp_number"))
        self.assertEqual(numbers, {"Ali Raza": "923001234567", "Sara Khan": "923335556667"})
        for patient in Patient.objects.all():
            self.assertIsNotNone(patient.created_at)
            self.assertIsNotNone(patient.updated_at)
            self.assertEqual(patient.created_by, self.owner)

    def test_a_long_file_runs_a_fixed_number_of_queries(self):
        """Regression: saving ran about 5 queries per patient, so a 5,000-row file hit the server timeout."""

        def import_queries(count, first):
            lines = [f"Patient {n},M,,,0300-{1000000 + n},,,,,,,," for n in range(first, first + count)]
            with CaptureQueriesContext(connection) as queries:
                response = self.post(csv_upload(rows(*lines)))
            self.assertEqual(response.status_code, 302)
            patient_inserts = [q for q in queries if q["sql"].startswith('INSERT INTO "patients_patient"')]
            return len(queries) - len(patient_inserts), len(patient_inserts)

        small_other, _ = import_queries(5, 0)
        big_other, big_inserts = import_queries(600, 1000)
        self.assertEqual(Patient.objects.count(), 605)
        self.assertEqual(big_other, small_other, "only the number of INSERT batches may grow with the file")
        # Batches of up to 500 rows (SQLite, used by the tests, fits about 45 patients per INSERT).
        self.assertLess(big_inserts, 20)

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


class ImportDuplicateTests(ClinicTestCase):
    """Rows that look like a patient who is already registered stop the import until staff choose."""

    def setUp(self):
        self.login(self.owner)

    def post(self, upload, duplicates=""):
        return self.client.post(IMPORT_URL, {"file": upload, "duplicates": duplicates})

    def test_importing_the_same_file_twice_does_not_double_the_register(self):
        text = rows("Ayesha Khan,F,1988-04-15,,0300-1234567,,,,,,,,", "Bilal Ahmed,M,,,0321 7654321,,,,,,,,")
        self.assertEqual(self.post(csv_upload(text)).status_code, 302)
        ayesha, bilal = Patient.objects.order_by("full_name")

        response = self.post(csv_upload(text))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Patient.objects.count(), 2)
        self.assertEqual(AuditLog.objects.filter(action=AuditLog.Action.IMPORT).count(), 1)
        self.assertEqual(response.context["errors"], [])
        self.assertEqual(
            response.context["duplicates"],
            [
                (2, f"Ayesha Khan looks already registered as {ayesha.mrn} (same name and date of birth)."),
                (3, f"Bilal Ahmed looks already registered as {bilal.mrn} (same name and mobile number)."),
            ],
        )
        self.assertContains(response, "Nothing was imported yet")
        self.assertContains(response, 'name="duplicates"')

    def test_choice_is_only_offered_after_duplicates_were_found(self):
        self.assertNotContains(self.client.get(IMPORT_URL), 'name="duplicates"')
        response = self.post(csv_upload(rows("Bad,Q,,,0300-1234567,,,,,,,,")))
        self.assertNotContains(response, 'name="duplicates"')

    def test_same_patient_twice_in_one_file(self):
        response = self.post(csv_upload(rows(
            "Ayesha Khan,F,,,0300-1234567,,,,,,,,",
            "Sara Khan,F,15/04/1990,,0300-1234568,,,,,,,,",
            "ayesha  KHAN,F,,,+92 300 1234567,,,,,,,,",  # other case, spacing and number format
            "SARA KHAN,F,1990-04-15,,,,,,,,,,",
        )))
        self.assertEqual(
            response.context["duplicates"],
            [
                (4, "Same patient as row 2 (same name and mobile number)."),
                (5, "Same patient as row 3 (same name and date of birth)."),
            ],
        )
        self.assertFalse(Patient.objects.exists())

    def test_patient_added_by_hand_then_imported_is_flagged(self):
        existing = self.make_patient(full_name="Ayesha Khan", phone="0300-1234567", date_of_birth=None)
        response = self.post(csv_upload(rows("AYESHA KHAN,F,,,03001234567,,,,,,,,")))
        self.assertEqual(
            response.context["duplicates"],
            [(2, f"AYESHA KHAN looks already registered as {existing.mrn} (same name and mobile number).")],
        )
        self.assertEqual(Patient.objects.count(), 1)

    def test_archived_and_other_clinics_patients_do_not_count(self):
        self.make_patient(full_name="Ayesha Khan", phone="0300-1234567", is_archived=True)
        make_patient(self.other_clinic, full_name="Ayesha Khan", phone="0300-1234567")
        response = self.post(csv_upload(rows("Ayesha Khan,F,,,0300-1234567,,,,,,,,")))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Patient.objects.filter(clinic=self.clinic, is_archived=False).count(), 1)

    def test_family_sharing_one_mobile_is_imported(self):
        self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        response = self.post(csv_upload(rows(
            "Imran Khan,M,,,0300-1234567,,,,,,,,",
            "Zara Khan,F,,8,0300-1234567,,,,,,,,",
        )))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Patient.objects.filter(whatsapp_number="923001234567").count(), 3)

    def test_import_anyway(self):
        self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        upload = rows("Ayesha Khan,F,,,0300-1234567,,,,,,,,", "Bilal Ahmed,M,,,0321 7654321,,,,,,,,")
        response = self.post(csv_upload(upload), duplicates="import")
        self.assertRedirects(response, f"{reverse('patients:list')}?sort=recent", fetch_redirect_response=False)
        self.assertEqual(Patient.objects.filter(full_name="Ayesha Khan").count(), 2)
        self.assertEqual(Patient.objects.count(), 3)
        self.assertEqual([str(m) for m in get_messages(response.wsgi_request)], ["Imported 2 patients."])
        log = AuditLog.objects.get(action=AuditLog.Action.IMPORT)
        self.assertEqual(log.summary, "Imported 2 patients from CSV")

    def test_skip_the_ones_already_registered(self):
        existing = self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        upload = rows(
            "Ayesha Khan,F,,,0300-1234567,,,,,,,,",
            "Bilal Ahmed,M,,,0321 7654321,,,,,,,,",
            "Bilal Ahmed,M,,,0321-7654321,,,,,,,,",  # same as the row above: the first one is kept
        )
        response = self.post(csv_upload(upload), duplicates="skip")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Patient.objects.filter(full_name="Ayesha Khan")), [existing])
        self.assertEqual(Patient.objects.filter(full_name="Bilal Ahmed").count(), 1)
        self.assertEqual(
            [str(m) for m in get_messages(response.wsgi_request)],
            ["Imported 1 patient. Skipped 2 who were already registered."],
        )
        log = AuditLog.objects.get(action=AuditLog.Action.IMPORT)
        self.assertEqual(log.summary, "Imported 1 patient from CSV, skipped 2 already registered")

    def test_a_choice_never_skips_rows_that_need_fixing(self):
        self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        upload = rows("Ayesha Khan,F,,,0300-1234567,,,,,,,,", "Bad Sex,X,,,0300-1234569,,,,,,,,")
        for choice in ("skip", "import"):
            with self.subTest(choice=choice):
                response = self.post(csv_upload(upload), duplicates=choice)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row for row, _ in response.context["errors"]], [3])
                self.assertEqual([row for row, _ in response.context["duplicates"]], [2])
                self.assertContains(response, "Also check these patients")
        self.assertEqual(Patient.objects.count(), 1)

    def test_parser_reports_duplicates_apart_from_errors(self):
        self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        result = importer.parse_patients_csv(
            rows("Ayesha Khan,F,,,0300-1234567,,,,,,,,", "Bilal Ahmed,M,,,0321 7654321,,,,,,,,").encode(), self.clinic
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.errors, [])
        self.assertEqual([row for row, _ in result.duplicates], [2])
        self.assertEqual([p.full_name for p in result.patients], ["Ayesha Khan", "Bilal Ahmed"])
        self.assertEqual([p.full_name for p in result.new_patients], ["Bilal Ahmed"])
