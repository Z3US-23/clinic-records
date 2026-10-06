"""Visit detail, visit list and the printable prescription."""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import dateformat, timezone

from apps.accounts.models import Clinic, Membership
from apps.clinical.models import LabResult, Visit
from apps.core.audit import Action
from apps.core.testing import make_patient

from .base import ClinicalTestCase


def detail_url(visit):
    return reverse("clinical:visit_detail", args=[visit.pk])


def print_url(visit):
    return reverse("clinical:prescription_print", args=[visit.pk])


LIST_URL = reverse("clinical:visit_list")


class VisitDetailTests(ClinicalTestCase):
    def setUp(self):
        self.visit = self.make_visit(
            self.patient,
            chief_complaint="Headache for a week",
            history="Worse in the mornings\nNo vomiting",
            examination="BP raised",
            diagnosis="Hypertension",
            plan="Reduce salt",
            pulse=82,
            weight_kg=Decimal("70"),
            height_cm=Decimal("175"),
            follow_up_date=timezone.localdate() + timedelta(days=14),
        )
        self.add_items(self.visit, "Tab. Amlodipine 5mg")

    def test_clinicians_see_the_visit(self):
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(detail_url(self.visit))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "clinical/visit_detail.html")
                self.assertContains(response, "Allergies: Penicillin")
                self.assertContains(response, "Seen by Dr. Bilal Hussain")
                self.assertContains(response, "Headache for a week")
                self.assertContains(response, "Worse in the mornings\nNo vomiting")  # kept as typed (pre-line)
                self.assertContains(response, "Hypertension")
                self.assertContains(response, "Reduce salt")
                self.assertContains(response, "Tab. Amlodipine 5mg")
                self.assertContains(response, print_url(self.visit))
                lab_url = reverse("clinical:lab_create", args=[self.patient.pk]) + f"?visit={self.visit.pk}"
                self.assertContains(response, lab_url)

    def test_viewing_is_audited(self):
        self.login(self.doctor)
        self.client.get(detail_url(self.visit))
        self.assertTrue(self.audit_exists(Action.VIEW, self.visit, f"Viewed visit for {self.patient.mrn}"))

    def test_only_filled_vitals_are_shown_with_bmi(self):
        self.login(self.doctor)
        response = self.client.get(detail_url(self.visit))
        labels = [vital["label"] for vital in response.context["vitals"]]
        self.assertEqual(labels, ["Pulse", "Weight", "Height", "BMI"])
        self.assertContains(response, "82 bpm")
        self.assertContains(response, "22.9")  # 70 kg / 1.75 m²
        self.assertNotContains(response, "SpO₂")

    def test_no_vitals_card_when_none_were_taken(self):
        visit = self.make_visit(self.patient)
        self.login(self.doctor)
        response = self.client.get(detail_url(visit))
        self.assertEqual(response.context["vitals"], [])
        self.assertNotContains(response, ">Vitals<")

    def test_edit_button_only_for_the_visit_doctor_and_owner(self):
        edit_url = reverse("clinical:visit_update", args=[self.visit.pk])
        for user, can_edit in [(self.doctor, True), (self.owner, True), (self.second_doctor, False)]:
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(detail_url(self.visit))
                self.assertEqual(response.status_code, 200)
                if can_edit:
                    self.assertContains(response, edit_url)
                else:
                    self.assertNotContains(response, edit_url)

    def test_book_follow_up_link(self):
        self.login(self.doctor)
        response = self.client.get(detail_url(self.visit))
        expected = (
            reverse("appointments:create")
            + f"?patient={self.patient.pk}&amp;date={self.visit.follow_up_date:%Y-%m-%d}"
        )
        self.assertContains(response, expected)
        self.assertContains(response, "Book follow-up")

    def test_lab_results_of_this_visit_are_listed(self):
        LabResult.objects.create(clinic=self.clinic, patient=self.patient, visit=self.visit, test_name="Lipid profile")
        LabResult.objects.create(clinic=self.clinic, patient=self.patient, test_name="Unrelated X-ray")
        self.login(self.doctor)
        response = self.client.get(detail_url(self.visit))
        self.assertContains(response, "Lipid profile")
        self.assertNotContains(response, "Unrelated X-ray")

    def test_user_text_is_escaped(self):
        visit = self.make_visit(self.patient, chief_complaint="<script>alert(1)</script>")
        self.login(self.doctor)
        response = self.client.get(detail_url(visit))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(detail_url(self.visit)).status_code, 403)

    def test_other_clinic_gets_not_found(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.get(detail_url(self.visit)).status_code, 404)

    def test_signed_out_user_goes_to_login(self):
        response = self.client.get(detail_url(self.visit))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


class VisitListTests(ClinicalTestCase):
    def setUp(self):
        now = timezone.now()
        self.usman = make_patient(self.clinic, full_name="Usman Tariq")
        self.old = self.make_visit(self.patient, visit_date=now - timedelta(days=40), diagnosis="Old one")
        self.recent = self.make_visit(
            self.usman, doctor=self.second_doctor, visit_date=now - timedelta(days=2), diagnosis="Recent one"
        )
        self.today = self.make_visit(self.patient, visit_date=now, chief_complaint="Today's complaint")
        self.elsewhere = self.make_visit(self.other_patient, doctor=self.other_owner, chief_complaint="Elsewhere")

    def listed(self, response):
        return list(response.context["visits"])

    def test_lists_this_clinics_visits_newest_first(self):
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(LIST_URL)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "clinical/visit_list.html")
                self.assertEqual(self.listed(response), [self.today, self.recent, self.old])
                self.assertNotContains(response, "Elsewhere")
                self.assertContains(response, 'data-label="Diagnosis"')  # stacks on phones

    def test_filter_by_doctor(self):
        self.login(self.doctor)
        response = self.client.get(LIST_URL, {"doctor": self.second_doctor.pk})
        self.assertEqual(self.listed(response), [self.recent])

    def test_doctor_choices_are_this_clinics_doctors(self):
        self.login(self.doctor)
        response = self.client.get(LIST_URL)
        doctors = set(response.context["filter_form"].fields["doctor"].queryset)
        self.assertEqual(doctors, {self.owner, self.doctor, self.second_doctor})

    def test_filter_by_patient_name_or_mrn(self):
        self.login(self.doctor)
        self.assertEqual(self.listed(self.client.get(LIST_URL, {"q": "usman"})), [self.recent])
        self.assertEqual(self.listed(self.client.get(LIST_URL, {"q": self.usman.mrn})), [self.recent])
        self.assertEqual(self.listed(self.client.get(LIST_URL, {"q": "Zara"})), [])  # other clinic's patient

    def test_filter_by_dates(self):
        self.login(self.doctor)
        today = timezone.localdate()
        response = self.client.get(LIST_URL, {"date_from": (today - timedelta(days=3)).isoformat()})
        self.assertEqual(self.listed(response), [self.today, self.recent])
        response = self.client.get(LIST_URL, {"date_to": (today - timedelta(days=30)).isoformat()})
        self.assertEqual(self.listed(response), [self.old])

    def test_invalid_filters_are_ignored(self):
        self.login(self.doctor)
        bad = {
            "doctor": "abc",
            "date_from": "not-a-date",
            "date_to": "2026-13-45",
            "page": "xyz",
            "q": "",
        }
        response = self.client.get(LIST_URL, bad)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.listed(response), [self.today, self.recent, self.old])

        # A doctor from another clinic is not a valid choice: ignored, never leaked.
        response = self.client.get(LIST_URL, {"doctor": self.other_owner.pk, "page": "999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.listed(response)), 3)

    def test_pages_of_25(self):
        for n in range(27):
            self.make_visit(self.usman, visit_date=timezone.now() - timedelta(days=100 + n))
        self.login(self.doctor)
        first = self.client.get(LIST_URL)
        self.assertEqual(len(self.listed(first)), 25)
        self.assertContains(first, "Next")
        second = self.client.get(LIST_URL, {"page": 2})
        self.assertEqual(len(self.listed(second)), 5)

    def test_empty_states(self):
        self.login(self.doctor)
        response = self.client.get(LIST_URL, {"q": "nobody by this name"})
        self.assertContains(response, "No visits match these filters")
        Visit.objects.filter(clinic=self.clinic).delete()
        response = self.client.get(LIST_URL)
        self.assertContains(response, "No visits yet")

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)


class PrescriptionPrintTests(ClinicalTestCase):
    def setUp(self):
        Clinic.objects.filter(pk=self.clinic.pk).update(
            address="12 Mall Road",
            city="Lahore",
            phone="042-1234567",
            prescription_header="Timings: 5–9 pm\nClosed on Sundays",
            prescription_footer="Bring this slip on your next visit",
        )
        Membership.objects.filter(user=self.doctor, clinic=self.clinic).update(
            qualifications="MBBS, FCPS (Medicine)", registration_number="PMDC-12345"
        )
        self.visit = self.make_visit(
            self.patient,
            diagnosis="Acute pharyngitis",
            plan="Warm salt-water gargles",
            bp_systolic=120,
            bp_diastolic=80,
            follow_up_date=timezone.localdate() + timedelta(days=7),
        )
        self.add_items(self.visit, "Tab. Augmentin 625mg", "Tab. Panadol 500mg")

    def test_printable_prescription(self):
        self.login(self.doctor)
        response = self.client.get(print_url(self.visit))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "clinical/prescription_print.html")
        self.assertTemplateNotUsed(response, "base.html")  # standalone page: no menus

        for text in [
            "Al-Noor Family Clinic",
            "12 Mall Road",
            "Lahore",
            "042-1234567",
            "Timings: 5–9 pm\nClosed on Sundays",
            "Dr. Bilal Hussain",
            "MBBS, FCPS (Medicine)",
            "Reg. No. PMDC-12345",
            "Ayesha Khan",
            self.patient.mrn,
            f"{self.patient.age_display} / Female",
            "BP 120/80 mmHg",
            "Acute pharyngitis",
            "Warm salt-water gargles",
            "Penicillin",
            "Bring this slip on your next visit",
            "Signature",
        ]:
            self.assertContains(response, text)
        self.assertContains(response, "<strong>Tab. Augmentin 625mg</strong>", html=True)
        self.assertContains(response, "1 tablet · 1+0+1")
        self.assertContains(response, "&#8478;")  # the Rx symbol
        follow_up = dateformat.format(self.visit.follow_up_date, "l, j M Y")
        self.assertContains(response, f"please come back on {follow_up}")
        self.assertContains(response, "css/clinical.css")
        self.assertContains(response, "js/clinical.js")
        self.assertContains(response, "data-print")
        self.assertContains(response, detail_url(self.visit))  # "Back to visit"

    def test_owner_printing_shows_the_visit_doctor(self):
        self.login(self.owner)
        response = self.client.get(print_url(self.visit))
        self.assertContains(response, "Dr. Bilal Hussain")
        self.assertContains(response, "PMDC-12345")

    def test_printing_is_audited(self):
        self.login(self.doctor)
        self.client.get(print_url(self.visit))
        self.assertTrue(self.audit_exists(Action.VIEW, self.visit, f"Printed prescription for {self.patient.mrn}"))

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(print_url(self.visit)).status_code, 403)

    def test_other_clinic_gets_not_found(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.get(print_url(self.visit)).status_code, 404)
