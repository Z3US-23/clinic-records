"""The shared page layout (base.html), includes and app.js behaviour that every app relies on."""

import re
from datetime import timedelta

from django.contrib.staticfiles import finders
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from .base import CoreTestCase

LONG_ALLERGIES = (
    "Penicillin (rash and swelling), Sulfa drugs, NSAIDs including ibuprofen and diclofenac, "
    "Aspirin, Codeine, Latex"
)


class TopBarTests(CoreTestCase):
    def test_new_patient_button_keeps_its_name_on_phones(self):
        # On phones the label is hidden visually (icon only) but must still name the link for screen readers.
        self.login(self.receptionist)
        html = self.client.get(reverse("patients:list")).content.decode()
        link = re.search(r'<a [^>]*href="%s"[^>]*>.*?</a>' % re.escape(reverse("patients:create")), html, re.S)
        self.assertIsNotNone(link)
        self.assertIn('title="New patient"', link.group(0))
        self.assertIn('<span class="btn-label">New patient</span>', link.group(0))


class AllergyBannerTests(CoreTestCase):
    """The patient banner shows the whole allergy list to clinicians, and nothing to receptionists."""

    def setUp(self):
        super().setUp()
        self.assertGreater(len(LONG_ALLERGIES), 80)
        self.patient = self.make_patient(full_name="Ayesha Khan", allergies=LONG_ALLERGIES)
        self.visit = self.make_visit(self.patient, visit_date=timezone.now() - timedelta(hours=1))

    def test_clinicians_see_every_allergy(self):
        self.login(self.doctor)
        for url in (
            reverse("clinical:visit_detail", args=[self.visit.pk]),
            reverse("clinical:lab_create", args=[self.patient.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, '<div class="allergy-banner">')
                self.assertContains(response, "Codeine, Latex")  # the end of the list, past 80 characters

    def test_receptionists_never_see_allergies(self):
        appointment = self.make_appointment(self.patient)
        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:update", args=[appointment.pk]))
        self.assertContains(response, "patient-banner")
        self.assertNotContains(response, "Latex")
        self.assertNotContains(response, "allergy-banner")


class DoubleSubmitGuardTests(CoreTestCase):
    """app.js ignores a second tap on a POST form while the first is on its way."""

    def test_app_js_has_the_guard(self):
        with open(finders.find("js/app.js"), encoding="utf-8") as f:
            script = f.read()
        for part in ("form.dataset.submitting", "data-allow-resubmit", '"pageshow"'):
            self.assertIn(part, script)

    def test_export_download_opts_out(self):
        # The ZIP downloads and the page stays, so the button must work again straight away.
        self.login(self.owner)
        self.assertContains(self.client.get(reverse("core:export")), "data-allow-resubmit")


class FocusAndContrastTests(SimpleTestCase):
    """Guards for the accessibility fixes in app.css (contrast itself is checked by hand)."""

    def setUp(self):
        with open(finders.find("css/app.css"), encoding="utf-8") as f:
            self.css = f.read()

    def test_keyboard_focus_ring_is_a_solid_outline(self):
        self.assertIn(":focus-visible { outline: 2px solid var(--primary)", self.css)
        self.assertNotIn(":focus-visible { outline: none", self.css)

    def test_subtle_text_colours(self):
        self.assertIn("--text-subtle: #606d7b;", self.css)
        self.assertIn("--text-subtle: #8f9daa;", self.css)
