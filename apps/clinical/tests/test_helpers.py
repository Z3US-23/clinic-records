"""Small helpers: follow-up dates, vitals display, medicine suggestions, file checks, admin pages."""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.clinical.models import PrescriptionItem, Visit
from apps.clinical.templatetags.clinical_tags import rx_details
from apps.clinical.uploads import safe_filename, validate_lab_file
from apps.clinical.utils import add_months, follow_up_from, medicine_suggestions, vitals_for_display

from .base import HTML_BYTES, JPEG_BYTES, PDF_BYTES, PNG_BYTES, ClinicalTestCase, TempMediaMixin, upload


class FollowUpDateTests(SimpleTestCase):
    def test_quick_picks(self):
        day = date(2026, 10, 6)
        self.assertEqual(follow_up_from(day, "7d"), date(2026, 10, 13))
        self.assertEqual(follow_up_from(day, "14d"), date(2026, 10, 20))
        self.assertEqual(follow_up_from(day, "1m"), date(2026, 11, 6))
        self.assertEqual(follow_up_from(day, "3m"), date(2027, 1, 6))

    def test_months_stay_inside_short_months(self):
        self.assertEqual(add_months(date(2027, 1, 31), 1), date(2027, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))  # leap year
        self.assertEqual(add_months(date(2026, 11, 30), 3), date(2027, 2, 28))
        self.assertEqual(add_months(date(2026, 12, 15), 1), date(2027, 1, 15))


class DisplayTests(SimpleTestCase):
    def test_vitals_show_only_filled_values(self):
        visit = Visit(
            bp_systolic=120,
            bp_diastolic=80,
            temperature_c=Decimal("37.0"),
            spo2=98,
            weight_kg=Decimal("80.5"),
            height_cm=Decimal("170"),
        )
        shown = {vital["label"]: vital["text"] for vital in vitals_for_display(visit)}
        self.assertEqual(
            shown,
            {
                "BP": "120/80 mmHg",
                "Temp": "37 °C",
                "SpO₂": "98%",
                "Weight": "80.5 kg",
                "Height": "170 cm",
                "BMI": "27.9",
            },
        )

    def test_blood_pressure_needs_both_numbers(self):
        self.assertEqual(vitals_for_display(Visit(bp_systolic=120)), [])

    def test_rx_details_skips_blanks(self):
        item = PrescriptionItem(medicine="Tab. X", dose="1 tablet", frequency="", duration="5 days")
        self.assertEqual(rx_details(item), "1 tablet · 5 days")
        self.assertEqual(rx_details(PrescriptionItem(medicine="Tab. Y")), "")


class FileCheckTests(SimpleTestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename("HbA1c Oct.pdf"), "HbA1c Oct.pdf")
        self.assertEqual(safe_filename("C:\\fakepath\\my \"lab\";report.pdf"), "my labreport.pdf")
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("bad\x00\r\nname.pdf"), "badname.pdf")
        self.assertEqual(safe_filename(""), "lab-report")
        self.assertEqual(safe_filename("..."), "lab-report")

    def test_long_filename_is_cut_to_255_keeping_the_extension(self):
        name = safe_filename("a" * 400 + ".pdf")
        self.assertEqual(len(name), 255)
        self.assertTrue(name.endswith(".pdf"))

    def test_genuine_files_pass(self):
        for name, content in [("a.pdf", PDF_BYTES), ("a.png", PNG_BYTES), ("a.jpg", JPEG_BYTES), ("a.JPEG", JPEG_BYTES)]:
            with self.subTest(name=name):
                validate_lab_file(upload(name, content))

    def test_mismatched_or_empty_files_fail(self):
        for name, content, code in [
            ("a.pdf", HTML_BYTES, "signature"),
            ("a.pdf", PNG_BYTES, "signature"),
            ("a.gif", b"GIF89a", "extension"),
            ("a.pdf", b"", "empty"),
        ]:
            with self.subTest(name=name, code=code):
                with self.assertRaises(ValidationError) as caught:
                    validate_lab_file(upload(name, content))
                self.assertEqual(caught.exception.code, code)


class MedicineSuggestionTests(ClinicalTestCase):
    def test_distinct_names_from_this_clinic_most_used_first(self):
        first = self.make_visit(self.patient)
        second = self.make_visit(self.patient)
        self.add_items(first, "Tab. Panadol", "Tab. Brufen")
        self.add_items(second, "Tab. Panadol", "Cap. Omeprazole")
        other = self.make_visit(self.other_patient, doctor=self.other_owner)
        self.add_items(other, "Tab. Other")

        self.assertEqual(medicine_suggestions(self.clinic), ["Cap. Omeprazole", "Tab. Brufen", "Tab. Panadol"])
        # Capped: the most used names are kept.
        self.assertEqual(medicine_suggestions(self.clinic, limit=1), ["Tab. Panadol"])


class AdminTests(TempMediaMixin, ClinicalTestCase):
    """The platform operator's Django admin (superusers only)."""

    def test_admin_pages_render(self):
        admin_user = User.objects.create_superuser(email="admin@example.test", password="x-pass-12345", full_name="Admin")
        self.client.force_login(admin_user)
        visit = self.make_visit(self.patient)
        self.add_items(visit, "Tab. Panadol")
        lab = self.make_lab(visit=visit)

        for url in [
            reverse("admin:clinical_visit_changelist"),
            reverse("admin:clinical_visit_add"),
            reverse("admin:clinical_visit_change", args=[visit.pk]),
            reverse("admin:clinical_labresult_changelist"),
            reverse("admin:clinical_labresult_change", args=[lab.pk]),
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.get(reverse("admin:clinical_visit_change", args=[visit.pk]))
        self.assertContains(response, "Tab. Panadol")  # prescription inline

    def test_clinic_staff_cannot_use_the_admin(self):
        self.login(self.owner)
        response = self.client.get(reverse("admin:clinical_visit_changelist"))
        self.assertEqual(response.status_code, 302)
