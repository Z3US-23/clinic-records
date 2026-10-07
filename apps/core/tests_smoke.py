"""Smoke test for the whole app: every URL name, opened by every kind of user.

It catches the problems that slip between apps: a page that crashes, a link to a
URL name that no longer exists, a page left public by mistake, a role that sees
too much, or one clinic reaching another clinic's records.

For each URL in PAGES it checks:
  * owner, doctor and receptionist get the status the roles table in
    docs/ARCHITECTURE.md promises (403 for pages their role may not open);
  * nothing ever answers with a server error (5xx) or "not built yet" (501);
  * the owner of ANOTHER clinic gets 404 on every record of this clinic;
  * POST-only actions refuse GET with 405;
  * signed-out visitors are sent to the sign-in page, except on the public pages
    (patient confirmation link, offline page, app manifest, service worker).

When you add a URL, add it to PAGES: test_every_url_name_is_covered fails until you do.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from apps.accounts.models import Membership
from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem
from apps.clinical.tests.base import TempMediaMixin, upload
from apps.core.testing import ClinicTestCase, make_appointment, make_patient, make_visit
from apps.reminders.models import Reminder

# Who may open a page (see the roles table in docs/ARCHITECTURE.md).
PUBLIC = "public"  # no sign-in needed
ANY_ROLE = "any role"  # any signed-in staff member of a clinic
CLINICIANS = "clinicians"  # owner and doctor
OWNER = "owner"

# The namespaces whose URL names make up the contract.
APP_NAMESPACES = {"core", "accounts", "patients", "clinical", "appointments", "public", "reminders"}

# Words that only appear in this test's clinical records. A receptionist must never see them,
# and the other clinic's owner must never see this clinic's patient.
CLINICAL_MARKERS = ("SMOKE-DIAGNOSIS", "SMOKE-ALLERGY", "SMOKE-CONDITION", "SMOKE-MEDICINE", "SMOKE-LAB")
PATIENT_NAME = "Smoketest Patient"


@dataclass(frozen=True)
class Page:
    """One URL to open. `args` and `query` use the names of the records made in setUpTestData."""

    name: str
    args: tuple = ()
    query: str = ""
    access: str = ANY_ROLE
    post_only: bool = False
    # True when the URL points at one of this clinic's records: another clinic gets 404.
    clinic_record: bool = False
    # What an allowed, signed-in user gets on GET (most pages: 200).
    status: int = 200


PAGES = [
    # --- core ---
    Page("core:dashboard"),
    Page("core:missed_follow_ups"),
    Page("core:hide_setup_checklist", access=OWNER, post_only=True),
    Page("core:audit_log", access=OWNER),
    Page("core:export", access=OWNER),
    Page("core:manifest", access=PUBLIC),
    Page("core:service_worker", access=PUBLIC),
    Page("core:offline", access=PUBLIC),
    # --- accounts ---
    Page("accounts:login", access=PUBLIC, status=302),  # signed-in users are sent to Today
    Page("accounts:signup", access=PUBLIC, status=302),  # signed-in users are sent to Today
    Page("accounts:logout", post_only=True),
    Page("accounts:password_change"),
    Page("accounts:password_change_done"),
    Page("accounts:no_clinic", status=302),  # they do have a clinic, so: Today
    Page("accounts:switch_clinic", args=("clinic",), post_only=True, clinic_record=True),
    # Someone else's (or a made-up) invitation link: 404, so nothing leaks.
    Page("accounts:join", args=("invitation",), status=404),
    Page("accounts:profile"),
    Page("accounts:staff_list", access=OWNER),
    Page("accounts:staff_add", access=OWNER),
    Page("accounts:staff_edit", args=("membership",), access=OWNER, clinic_record=True),
    Page("accounts:staff_set_password", args=("membership",), access=OWNER, clinic_record=True),
    Page("accounts:staff_invite", args=("membership",), access=OWNER, post_only=True, clinic_record=True),
    Page("accounts:clinic_settings", access=OWNER),
    # --- patients ---
    Page("patients:list"),
    Page("patients:list", query="?q=Smoketest"),
    Page("patients:search_json", query="?q=Smoketest"),
    Page("patients:create"),
    Page("patients:import", access=OWNER),
    Page("patients:import_template", access=OWNER),
    Page("patients:detail", args=("patient",), clinic_record=True),
    Page("patients:detail", args=("patient",), query="?tab=history", clinic_record=True),
    Page("patients:detail", args=("patient",), query="?tab=appointments", clinic_record=True),
    Page("patients:detail", args=("patient",), query="?tab=labs", clinic_record=True),
    Page("patients:detail", args=("patient",), query="?tab=messages", clinic_record=True),
    Page("patients:update", args=("patient",), clinic_record=True),
    Page("patients:archive", args=("patient",), access=CLINICIANS, post_only=True, clinic_record=True),
    # --- clinical ---
    Page("clinical:visit_list", access=CLINICIANS),
    Page("clinical:visit_create", args=("patient",), access=CLINICIANS, clinic_record=True),
    Page("clinical:visit_create", args=("patient",), query="?appointment={appointment}",
         access=CLINICIANS, clinic_record=True),
    Page("clinical:visit_detail", args=("visit",), access=CLINICIANS, clinic_record=True),
    Page("clinical:visit_update", args=("visit",), access=CLINICIANS, clinic_record=True),
    Page("clinical:prescription_print", args=("visit",), access=CLINICIANS, clinic_record=True),
    Page("clinical:lab_create", args=("patient",), access=CLINICIANS, clinic_record=True),
    Page("clinical:lab_create", args=("patient",), query="?visit={visit}", access=CLINICIANS, clinic_record=True),
    Page("clinical:lab_update", args=("lab",), access=CLINICIANS, clinic_record=True),
    Page("clinical:lab_file", args=("lab_with_file",), access=CLINICIANS, clinic_record=True),
    Page("clinical:lab_file", args=("lab",), access=CLINICIANS, clinic_record=True, status=404),  # no file
    Page("clinical:lab_delete", args=("lab",), access=CLINICIANS, post_only=True, clinic_record=True),
    # --- appointments ---
    Page("appointments:day"),
    Page("appointments:day", query="?date={tomorrow}"),
    Page("appointments:week"),
    Page("appointments:create"),
    Page("appointments:create", query="?patient={patient}&date={tomorrow}", clinic_record=True),
    Page("appointments:update", args=("appointment",), clinic_record=True),
    Page("appointments:set_status", args=("appointment",), post_only=True, clinic_record=True),
    Page("public:confirm", args=("token",), access=PUBLIC),
    # --- reminders ---
    Page("reminders:list"),
    Page("reminders:list", query="?tab=upcoming"),
    Page("reminders:list", query="?tab=sent"),
    Page("reminders:list", query="?tab=skipped"),
    Page("reminders:create"),
    Page("reminders:create", query="?patient={patient}", clinic_record=True),
    Page("reminders:templates", access=OWNER),
    Page("reminders:send", args=("reminder",), post_only=True, clinic_record=True),
    Page("reminders:skip", args=("reminder",), post_only=True, clinic_record=True),
    Page("reminders:update", args=("reminder",), clinic_record=True),
]


def contract_url_names():
    """Every named URL in the app namespaces, e.g. {"patients:detail", ...}."""
    names = set()
    for namespace, (_prefix, resolver) in get_resolver().namespace_dict.items():
        if namespace in APP_NAMESPACES:
            names.update(f"{namespace}:{key}" for key in resolver.reverse_dict if isinstance(key, str))
    return names


@override_settings(ALLOW_CLINIC_SIGNUP=True)
class SmokeTests(TempMediaMixin, ClinicTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        cls.patient = make_patient(
            cls.clinic, full_name=PATIENT_NAME, allergies="SMOKE-ALLERGY", chronic_conditions="SMOKE-CONDITION"
        )
        cls.visit = make_visit(
            cls.patient, cls.doctor, diagnosis="SMOKE-DIAGNOSIS", follow_up_date=timezone.localdate() + timedelta(days=1)
        )
        PrescriptionItem.objects.create(visit=cls.visit, medicine="SMOKE-MEDICINE 500mg", dose="1 tablet", frequency="1+0+1")
        cls.lab = LabResult.objects.create(
            clinic=cls.clinic, patient=cls.patient, visit=cls.visit, test_name="SMOKE-LAB", result_text="7.1%"
        )
        cls.lab_with_file = LabResult.objects.create(
            clinic=cls.clinic, patient=cls.patient, test_name="SMOKE-LAB report", file=upload("report.pdf"),
            original_filename="report.pdf",
        )
        cls.appointment = make_appointment(cls.patient, cls.doctor, when=now + timedelta(days=1), reason="Check-up")
        # Fill Today's "asking for another time" card and the core:missed_follow_ups list, so the
        # receptionist checks below also cover them.
        make_appointment(
            cls.patient, cls.doctor, when=now + timedelta(days=2),
            status=Appointment.Status.RESCHEDULE_REQUESTED, patient_note="Evening please",
        )
        missed = make_patient(cls.clinic, full_name="Smoketest Missed", phone="0300-7654321", allergies="SMOKE-ALLERGY")
        make_visit(
            missed, cls.doctor, diagnosis="SMOKE-DIAGNOSIS", visit_date=now - timedelta(days=40),
            follow_up_date=timezone.localdate() - timedelta(days=10),
        )
        cls.reminder = Reminder.objects.create(
            clinic=cls.clinic, patient=cls.patient, kind=Reminder.Kind.CUSTOM,
            due_date=timezone.localdate(), message="Hello, please call the clinic.",
        )
        cls.doctor_membership = Membership.objects.get(user=cls.doctor, clinic=cls.clinic)

    def setUp(self):
        super().setUp()
        # These tests expect hundreds of 403 / 404 / 405 answers. Keep Django's warning for each
        # out of the test output; real server errors are logged at ERROR and still show.
        request_log = logging.getLogger("django.request")
        self.addCleanup(request_log.setLevel, request_log.level)
        request_log.setLevel(logging.ERROR)

    # --- helpers ---------------------------------------------------------------

    def ids(self):
        return {
            "patient": self.patient.pk,
            "visit": self.visit.pk,
            "lab": self.lab.pk,
            "lab_with_file": self.lab_with_file.pk,
            "appointment": self.appointment.pk,
            "reminder": self.reminder.pk,
            "membership": self.doctor_membership.pk,
            "clinic": self.clinic.pk,
            "token": self.appointment.confirm_token,
            "invitation": "not-a-real-invitation",
            "tomorrow": (timezone.localdate() + timedelta(days=1)).isoformat(),
        }

    def url(self, page):
        ids = self.ids()
        return reverse(page.name, args=[ids[arg] for arg in page.args]) + page.query.format(**ids)

    @staticmethod
    def allowed(page, role):
        if page.access in (PUBLIC, ANY_ROLE):
            return True
        if page.access == CLINICIANS:
            return role in Membership.CLINICAL_ROLES
        return role == Membership.Role.OWNER

    def get(self, page):
        response = self.client.get(self.url(page))
        self.assertLess(response.status_code, 500, f"{page.name}{page.query}: server error")
        self.assertNotEqual(response.status_code, 501, f"{page.name}{page.query}: not built yet")
        return response

    # --- tests -----------------------------------------------------------------

    def test_every_url_name_is_covered(self):
        missing = contract_url_names() - {page.name for page in PAGES}
        self.assertFalse(missing, f"Add these URL names to PAGES in {__name__}: {sorted(missing)}")

    def test_every_page_for_every_role(self):
        users = {
            Membership.Role.OWNER: self.owner,
            Membership.Role.DOCTOR: self.doctor,
            Membership.Role.RECEPTIONIST: self.receptionist,
        }
        for role, user in users.items():
            self.client.force_login(user)
            for page in PAGES:
                with self.subTest(role=role, page=page.name, query=page.query):
                    response = self.get(page)
                    if page.post_only:
                        # Refused without changing anything: 405 once past the role check, 403 before it.
                        expected = {405} if self.allowed(page, role) else {403, 405}
                    elif self.allowed(page, role):
                        expected = {page.status}
                    else:
                        expected = {403}
                    self.assertIn(response.status_code, expected)

                    if response.status_code == 403:
                        self.assertContains(response, "access to this page", status_code=403)
                    if role == Membership.Role.RECEPTIONIST and response.status_code == 200:
                        body = response.content.decode(errors="replace")
                        for marker in CLINICAL_MARKERS:
                            self.assertNotIn(marker, body, f"Receptionist saw clinical data on {page.name}")

    def test_another_clinics_owner_gets_404_on_every_record(self):
        self.client.force_login(self.other_owner)
        for page in PAGES:
            with self.subTest(page=page.name, query=page.query):
                if page.clinic_record:
                    if page.post_only:
                        response = self.client.post(self.url(page), {"status": "cancelled"})
                    else:
                        response = self.get(page)
                    self.assertEqual(response.status_code, 404)
                else:
                    # Their own clinic's pages work, and never show this clinic's patient.
                    response = self.get(page)
                    self.assertNotIn(PATIENT_NAME, response.content.decode(errors="replace"))

        # The POSTs above changed nothing here.
        self.patient.refresh_from_db()
        self.appointment.refresh_from_db()
        self.reminder.refresh_from_db()
        self.assertFalse(self.patient.is_archived)
        self.assertEqual(self.appointment.status, "scheduled")
        self.assertEqual(self.reminder.status, Reminder.Status.PENDING)
        self.assertTrue(LabResult.objects.filter(pk=self.lab.pk).exists())

    def test_signed_out_visitors_only_reach_public_pages(self):
        login_url = reverse(settings.LOGIN_URL)
        for page in PAGES:
            with self.subTest(page=page.name, query=page.query):
                response = self.get(page)
                if page.access == PUBLIC:
                    self.assertEqual(response.status_code, 200)
                elif response.status_code == 405:
                    self.assertTrue(page.post_only)
                else:
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(login_url))

    def test_public_confirmation_link(self):
        page = Page("public:confirm", args=("token",), access=PUBLIC)
        response = self.get(page)
        self.assertContains(response, self.patient.first_name)
        self.assertNotContains(response, self.patient.mrn)
        for marker in CLINICAL_MARKERS:
            self.assertNotContains(response, marker)
        self.assertEqual(self.client.get(reverse("public:confirm", args=["not-a-real-token"])).status_code, 404)

    def test_installable_app_files_work_signed_out(self):
        for name, content_type in (
            ("core:manifest", "application/manifest+json"),
            ("core:service_worker", "application/javascript"),
            ("core:offline", "text/html"),
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response["Content-Type"].startswith(content_type))
