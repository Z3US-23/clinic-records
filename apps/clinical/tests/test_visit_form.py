"""New visit / edit visit: access rules, saving, prescription rows, appointments, follow-up."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem, Visit
from apps.clinical.utils import add_months
from apps.core.audit import Action
from apps.core.models import AuditLog
from apps.core.testing import make_patient
from apps.patients.services import clinical_summary
from apps.reminders.models import Reminder
from apps.reminders.services import generate_reminders

from .base import ClinicalTestCase, visit_post_data

REFRESH_FOR_VISIT = "apps.reminders.services.refresh_for_visit"

PARACETAMOL = {
    "medicine": "Tab. Paracetamol 500mg",
    "dose": "1 tablet",
    "frequency": "1+1+1",
    "duration": "3 days",
    "instructions": "After meals",
}
COUGH_SYRUP = {"medicine": "Syp. Hydryllin", "dose": "2 tsp", "frequency": "1+0+1", "duration": "5 days"}


def create_url(patient, **params):
    url = reverse("clinical:visit_create", args=[patient.pk])
    if params:
        url += "?" + "&".join(f"{key}={value}" for key, value in params.items())
    return url


def update_url(visit):
    return reverse("clinical:visit_update", args=[visit.pk])


def existing_rows(visit):
    """Formset rows for a visit's current medicines, as the edit page sends them back."""
    return [
        {"id": str(item.pk), "visit": str(visit.pk), "medicine": item.medicine, "dose": item.dose}
        for item in visit.prescription_items.all()
    ]


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def view_audits():
    """VIEW rows of the audit log (force_login also writes "Signed in" rows)."""
    return AuditLog.objects.filter(action=Action.VIEW)


class VisitCreateAccessTests(ClinicalTestCase):
    def test_clinicians_see_the_form(self):
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(create_url(self.patient))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "clinical/visit_form.html")
                self.assertContains(response, "Ayesha Khan")
                self.assert_allergy_banner(response, "Allergies: Penicillin")
                self.assertContains(response, 'name="items-TOTAL_FORMS" value="3"')  # 3 empty rows on create
                self.assertContains(response, 'class="formset-row"', count=4)  # 3 rows + the empty template row
                self.assertContains(response, 'id="medicine-options"')
                self.assertContains(response, "Add medicine")
                self.assertNotContains(response, "Remove")  # nothing to remove on a new visit
                for label in ("1 week", "2 weeks", "1 month", "3 months"):
                    self.assertContains(response, label)

    def test_receptionist_is_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(create_url(self.patient)).status_code, 403)
        response = self.client.post(create_url(self.patient), visit_post_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Visit.objects.exists())

    def test_other_clinics_patient_is_not_found(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(create_url(self.other_patient)).status_code, 404)
        response = self.client.post(create_url(self.other_patient), visit_post_data())
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Visit.objects.exists())

    def test_signed_out_user_goes_to_login(self):
        response = self.client.get(create_url(self.patient))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


@mock.patch(REFRESH_FOR_VISIT)
class VisitCreateTests(ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)

    def test_saves_visit_with_prescription_in_row_order(self, refresh):
        data = visit_post_data(
            rows=[PARACETAMOL, {"medicine": "", "dose": "1 tablet"}, COUGH_SYRUP],
            history="Started after a cold",
            bp_systolic="130",
            bp_diastolic="85",
            pulse="88",
            temperature_c="38.2",
            diagnosis="Acute bronchitis",
            plan="Steam inhalation",
        )
        response = self.client.post(create_url(self.patient), data)

        visit = Visit.objects.get()
        self.assertRedirects(response, visit.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(visit.clinic, self.clinic)
        self.assertEqual(visit.patient, self.patient)
        self.assertEqual(visit.doctor, self.doctor)
        self.assertEqual(visit.created_by, self.doctor)
        self.assertEqual(visit.blood_pressure, "130/85")
        self.assertEqual(str(visit.temperature_c), "38.2")
        self.assertEqual(visit.diagnosis, "Acute bronchitis")

        items = list(visit.prescription_items.all())
        self.assertEqual([item.medicine for item in items], ["Tab. Paracetamol 500mg", "Syp. Hydryllin"])
        self.assertEqual([item.order for item in items], [0, 1])  # the empty middle row was skipped
        self.assertEqual(items[0].instructions, "After meals")

        self.assertTrue(
            self.audit_exists(Action.CREATE, visit, f"Recorded visit for {self.patient.mrn}")
        )
        refresh.assert_called_once_with(visit)
        self.assertIn("Visit saved.", [str(m) for m in get_messages(response.wsgi_request)])

    def test_the_signed_in_clinician_is_the_doctor(self, refresh):
        self.login(self.owner)
        # A crafted "doctor" field is ignored: the form has no such field.
        self.client.post(create_url(self.patient), visit_post_data(doctor=str(self.second_doctor.pk)))
        visit = Visit.objects.get()
        self.assertEqual(visit.doctor, self.owner)
        self.assertEqual(visit.created_by, self.owner)

    def test_rows_without_a_medicine_are_ignored(self, refresh):
        rows = [{"medicine": "", "dose": "1 tablet", "frequency": "1+0+1"}, {"medicine": "   "}]
        response = self.client.post(create_url(self.patient), visit_post_data(rows=rows))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PrescriptionItem.objects.exists())

    def test_presenting_complaint_is_required(self, refresh):
        response = self.client.post(create_url(self.patient), visit_post_data(chief_complaint=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The visit was not saved")
        self.assertFalse(Visit.objects.exists())
        refresh.assert_not_called()

    def test_vitals_outside_the_allowed_range_are_rejected(self, refresh):
        for field, value in [("pulse", "999"), ("spo2", "120"), ("temperature_c", "60"), ("weight_kg", "0")]:
            with self.subTest(field=field):
                response = self.client.post(create_url(self.patient), visit_post_data(**{field: value}))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].has_error(field))
        self.assertFalse(Visit.objects.exists())

    def test_temperature_in_fahrenheit_is_saved_in_celsius(self, refresh):
        for typed, saved in [("101", "38.3"), ("98.6", "37.0"), ("104", "40.0"), ("100.4", "38.0")]:
            with self.subTest(typed=typed):
                Visit.objects.all().delete()
                response = self.client.post(create_url(self.patient), visit_post_data(temperature_c=typed))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(Visit.objects.get().temperature_c, Decimal(saved))

    def test_temperature_in_celsius_is_kept(self, refresh):
        for typed in ("38.3", "36.6", "30", "45"):
            with self.subTest(typed=typed):
                Visit.objects.all().delete()
                self.client.post(create_url(self.patient), visit_post_data(temperature_c=typed))
                self.assertEqual(Visit.objects.get().temperature_c, Decimal(typed))

    def test_temperature_outside_both_ranges_is_rejected(self, refresh):
        # 60 is neither a body temperature in °C (30–45) nor in °F (86–113).
        for typed in ("60", "29.9", "85", "114", "120", "-37"):
            with self.subTest(typed=typed):
                response = self.client.post(create_url(self.patient), visit_post_data(temperature_c=typed))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["form"].errors["temperature_c"],
                    ["Enter the temperature in °F (e.g. 101.2) or °C (e.g. 38.4)."],
                )
        self.assertFalse(Visit.objects.exists())

    def test_temperature_box_takes_fahrenheit(self, refresh):
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, "Temperature (°F or °C)")
        field = response.context["form"]["temperature_c"]
        self.assertEqual(field.field.widget.attrs["max"], 113)  # the browser must not block 101
        self.assertIn('max="113"', str(field))

    def test_blood_pressure_needs_both_numbers(self, refresh):
        response = self.client.post(create_url(self.patient), visit_post_data(bp_systolic="120"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("bp_diastolic"))

        response = self.client.post(create_url(self.patient), visit_post_data(bp_systolic="80", bp_diastolic="120"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("bp_systolic"))
        self.assertFalse(Visit.objects.exists())

    def test_follow_up_quick_pick_works_without_javascript(self, refresh):
        self.client.post(create_url(self.patient), visit_post_data(follow_up_in="14d"))
        visit = Visit.objects.get()
        self.assertEqual(visit.follow_up_date, timezone.localdate(visit.visit_date) + timedelta(days=14))

    def test_follow_up_quick_pick_in_months(self, refresh):
        self.client.post(create_url(self.patient), visit_post_data(follow_up_in="3m"))
        visit = Visit.objects.get()
        self.assertEqual(visit.follow_up_date, add_months(timezone.localdate(visit.visit_date), 3))

    def test_typed_follow_up_date_is_saved(self, refresh):
        day = timezone.localdate() + timedelta(days=10)
        self.client.post(create_url(self.patient), visit_post_data(follow_up_date=day.isoformat()))
        self.assertEqual(Visit.objects.get().follow_up_date, day)

    def test_follow_up_cannot_be_before_the_visit(self, refresh):
        yesterday = timezone.localdate() - timedelta(days=1)
        response = self.client.post(create_url(self.patient), visit_post_data(follow_up_date=yesterday.isoformat()))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("follow_up_date"))
        self.assertFalse(Visit.objects.exists())

    def test_unknown_quick_pick_is_rejected(self, refresh):
        response = self.client.post(create_url(self.patient), visit_post_data(follow_up_in="99y"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Visit.objects.exists())

    def test_at_most_40_medicines(self, refresh):
        rows = [{"medicine": f"Tab. Medicine {n}"} for n in range(41)]
        response = self.client.post(create_url(self.patient), visit_post_data(rows=rows))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 40 medicines")
        self.assertFalse(Visit.objects.exists())

    def test_reminder_problem_does_not_lose_the_visit(self, refresh):
        refresh.side_effect = RuntimeError("reminders broke")
        with self.assertLogs("apps.clinical.views", level="ERROR"):
            response = self.client.post(create_url(self.patient), visit_post_data(rows=[PARACETAMOL]))
        visit = Visit.objects.get()
        self.assertRedirects(response, visit.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(visit.prescription_items.count(), 1)

    def test_user_text_is_escaped_when_the_form_is_shown_again(self, refresh):
        response = self.client.post(
            create_url(self.patient), visit_post_data(history="<script>alert(1)</script>", pulse="999")
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")


@mock.patch(REFRESH_FOR_VISIT)
class VisitFromAppointmentTests(ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)
        self.appointment = self.make_appointment(self.patient, when=timezone.now())

    def test_appointment_is_linked_and_marked_seen(self, refresh):
        url = create_url(self.patient, appointment=self.appointment.pk)
        response = self.client.get(url)
        self.assertContains(response, "It will be marked as <strong>Seen</strong>", html=False)

        self.client.post(url, visit_post_data())
        visit = Visit.objects.get()
        self.assertEqual(visit.appointment, self.appointment)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.COMPLETED)
        self.assertTrue(
            AuditLog.objects.filter(
                action=Action.UPDATE, object_type="Appointment", object_id=str(self.appointment.pk)
            ).exists()
        )

    def test_cancelled_or_missed_appointment_is_not_marked_seen(self, refresh):
        for status in (Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW):
            with self.subTest(status=status):
                Visit.objects.all().delete()
                appointment = self.make_appointment(self.patient, when=timezone.now(), status=status)
                url = create_url(self.patient, appointment=appointment.pk)
                self.assertNotContains(self.client.get(url), "It will be marked as")

                self.client.post(url, visit_post_data())
                self.assertIsNone(Visit.objects.get().appointment)
                appointment.refresh_from_db()
                self.assertEqual(appointment.status, status)

    def test_appointment_of_another_patient_is_ignored(self, refresh):
        someone_else = make_patient(self.clinic, full_name="Usman Tariq")
        their_appointment = self.make_appointment(someone_else)
        self.client.post(create_url(self.patient, appointment=their_appointment.pk), visit_post_data())

        self.assertIsNone(Visit.objects.get().appointment)
        their_appointment.refresh_from_db()
        self.assertEqual(their_appointment.status, Appointment.Status.SCHEDULED)

    def test_appointment_of_another_clinic_is_ignored(self, refresh):
        other_appointment = self.make_appointment(self.other_patient, doctor=self.other_owner)
        self.client.post(create_url(self.patient, appointment=other_appointment.pk), visit_post_data())

        self.assertIsNone(Visit.objects.get().appointment)
        other_appointment.refresh_from_db()
        self.assertEqual(other_appointment.status, Appointment.Status.SCHEDULED)

    def test_garbled_appointment_id_is_ignored(self, refresh):
        for value in ("abc", "-1", "1.5", "9" * 40):
            with self.subTest(value=value):
                response = self.client.get(create_url(self.patient, appointment=value))
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["appointment"])


@mock.patch(REFRESH_FOR_VISIT)
class WaitingRoomAppointmentTests(ClinicalTestCase):
    """"New visit" opened without ?appointment= (e.g. from the patient's page) still closes
    the appointment the patient is waiting on today."""

    def setUp(self):
        self.login(self.doctor)

    def waiting(self, patient=None, when=None, **kwargs):
        kwargs.setdefault("status", Appointment.Status.ARRIVED)
        return self.make_appointment(patient or self.patient, when=when or timezone.now(), **kwargs)

    def assert_untouched(self, appointment, status=Appointment.Status.ARRIVED):
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, status)
        self.assertFalse(appointment.visits.exists())

    def test_todays_waiting_appointment_is_linked_and_marked_seen(self, refresh):
        appointment = self.waiting()
        response = self.client.get(create_url(self.patient))
        self.assertEqual(response.context["appointment"], appointment)
        self.assertContains(response, "It will be marked as <strong>Seen</strong>", html=False)

        response = self.client.post(create_url(self.patient), visit_post_data())
        visit = Visit.objects.get()
        self.assertRedirects(response, visit.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(visit.appointment, appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.COMPLETED)  # out of the waiting room

    def test_other_statuses_and_other_days_are_left_alone(self, refresh):
        booked_today = self.waiting(status=Appointment.Status.SCHEDULED)
        waited_yesterday = self.waiting(when=timezone.now() - timedelta(days=1))
        self.client.post(create_url(self.patient), visit_post_data())

        self.assertIsNone(Visit.objects.get().appointment)
        self.assert_untouched(booked_today, Appointment.Status.SCHEDULED)
        self.assert_untouched(waited_yesterday)

    def test_another_patients_or_clinics_appointment_is_never_linked(self, refresh):
        someone_else = self.waiting(patient=make_patient(self.clinic, full_name="Usman Tariq"))
        elsewhere = self.waiting(patient=self.other_patient, doctor=self.other_owner)
        self.client.post(create_url(self.patient), visit_post_data())

        self.assertIsNone(Visit.objects.get().appointment)
        self.assert_untouched(someone_else)
        self.assert_untouched(elsewhere)

    def test_an_appointment_with_this_doctor_is_preferred(self, refresh):
        self.waiting(doctor=self.second_doctor)  # arrived first, but for the other doctor
        mine = self.waiting(doctor=self.doctor)
        self.client.post(create_url(self.patient), visit_post_data())
        self.assertEqual(Visit.objects.get().appointment, mine)

    def test_any_doctors_appointment_is_used_when_there_is_no_other(self, refresh):
        theirs = self.waiting(doctor=self.second_doctor)
        self.client.post(create_url(self.patient), visit_post_data())
        self.assertEqual(Visit.objects.get().appointment, theirs)

    def test_an_explicit_appointment_wins(self, refresh):
        waiting = self.waiting()
        booked = self.make_appointment(self.patient, when=timezone.now() + timedelta(hours=1))
        self.client.post(create_url(self.patient, appointment=booked.pk), visit_post_data())

        self.assertEqual(Visit.objects.get().appointment, booked)
        self.assert_untouched(waiting)

    def test_an_unusable_explicit_appointment_is_not_swapped_for_a_guess(self, refresh):
        waiting = self.waiting()
        cancelled = self.waiting(status=Appointment.Status.CANCELLED)
        self.client.post(create_url(self.patient, appointment=cancelled.pk), visit_post_data())

        self.assertIsNone(Visit.objects.get().appointment)
        self.assert_untouched(waiting)

    def test_repeat_link_carries_the_waiting_appointment(self, refresh):
        appointment = self.waiting()
        earlier = self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=30))
        self.add_items(earlier, "Tab. Amlodipine 5mg")
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, f"copy_from={earlier.pk}&amp;appointment={appointment.pk}")

    def test_editing_a_visit_never_links_a_waiting_appointment(self, refresh):
        visit = self.make_visit(self.patient)
        appointment = self.waiting()
        self.client.post(update_url(visit), visit_post_data())

        visit.refresh_from_db()
        self.assertIsNone(visit.appointment)
        self.assert_untouched(appointment)


@mock.patch(REFRESH_FOR_VISIT)
class VisitFormAuditTests(ClinicalTestCase):
    """The visit form shows clinical data (earlier visits, labs, allergies), so opening it is audited."""

    def setUp(self):
        self.login(self.doctor)
        self.visit = self.make_visit(
            self.patient, visit_date=timezone.now() - timedelta(days=7), diagnosis="DIAGSECRET hypertension"
        )

    def assert_no_clinical_text_in_audit(self):
        for summary in AuditLog.objects.values_list("summary", flat=True):
            self.assertNotIn("DIAGSECRET", summary)
            self.assertNotIn("Penicillin", summary)

    def test_opening_a_new_visit_form_is_audited(self, refresh):
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, "DIAGSECRET")  # the previous visit is shown...
        audit = view_audits().get()  # ...so exactly one VIEW row is written
        self.assertEqual((audit.object_type, audit.object_id), ("Patient", str(self.patient.pk)))
        self.assertEqual(audit.summary, f"Opened new visit form for {self.patient.mrn}")
        self.assertEqual((audit.user, audit.clinic), (self.doctor, self.clinic))
        self.assert_no_clinical_text_in_audit()

    def test_opening_a_visit_for_editing_is_audited(self, refresh):
        response = self.client.get(update_url(self.visit))
        self.assertContains(response, "DIAGSECRET")
        self.assertEqual(view_audits().count(), 1)
        self.assertTrue(
            self.audit_exists(Action.VIEW, self.visit, f"Opened visit for editing for {self.patient.mrn}")
        )
        self.assert_no_clinical_text_in_audit()

    def test_a_save_is_audited_once_as_a_change_not_a_view(self, refresh):
        self.client.post(create_url(self.patient), visit_post_data(diagnosis="DIAGSECRET flu"))
        self.client.post(update_url(self.visit), visit_post_data(diagnosis="DIAGSECRET changed"))

        self.assertFalse(view_audits().exists())
        new_visit = Visit.objects.exclude(pk=self.visit.pk).get()
        self.assertTrue(self.audit_exists(Action.CREATE, new_visit, f"Recorded visit for {self.patient.mrn}"))
        self.assertTrue(self.audit_exists(Action.UPDATE, self.visit, f"Updated visit for {self.patient.mrn}"))
        self.assert_no_clinical_text_in_audit()

    def test_a_form_shown_again_with_errors_is_a_view(self, refresh):
        response = self.client.post(create_url(self.patient), visit_post_data(chief_complaint=""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(view_audits().count(), 1)
        self.assertFalse(AuditLog.objects.filter(action=Action.CREATE).exists())


@mock.patch(REFRESH_FOR_VISIT)
class VisitDateTests(ClinicalTestCase):
    """A visit can be put on an earlier day, to type in a patient's paper file."""

    PAPER_DAY = date(2024, 3, 1)

    def setUp(self):
        self.login(self.doctor)

    def test_new_visit_is_today_by_default(self, refresh):
        response = self.client.get(create_url(self.patient))
        today = timezone.localdate()
        self.assertContains(response, "Visit date")
        self.assertContains(response, f'name="seen_on" value="{today.isoformat()}"')
        self.assertContains(response, f'max="{today.isoformat()}"')  # the date picker stops at today
        self.assertContains(response, "Today · Dr. Bilal Hussain")

        self.client.post(create_url(self.patient), visit_post_data(seen_on=today.isoformat()))
        visit = Visit.objects.get()
        self.assertEqual(timezone.localdate(visit.visit_date), today)
        self.assertLess(timezone.now() - visit.visit_date, timedelta(minutes=1))  # "now", not a made-up time

    def test_empty_visit_date_means_today(self, refresh):
        self.client.post(create_url(self.patient), visit_post_data(seen_on=""))
        self.assertEqual(timezone.localdate(Visit.objects.get().visit_date), timezone.localdate())

    def test_back_dated_visit_with_its_old_follow_up(self, refresh):
        data = visit_post_data(seen_on=self.PAPER_DAY.isoformat(), follow_up_date="2024-03-15")
        response = self.client.post(create_url(self.patient), data)
        self.assertEqual(response.status_code, 302)
        visit = Visit.objects.get()
        self.assertEqual(timezone.localdate(visit.visit_date), self.PAPER_DAY)
        self.assertEqual(visit.follow_up_date, date(2024, 3, 15))  # in the past, but after the visit

    def test_follow_up_before_the_back_dated_day_is_rejected(self, refresh):
        data = visit_post_data(seen_on=self.PAPER_DAY.isoformat(), follow_up_date="2024-02-20")
        response = self.client.post(create_url(self.patient), data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("follow_up_date"))
        self.assertContains(response, "1 Mar 2024 · Dr. Bilal Hussain")  # the subtitle follows the chosen day
        self.assertFalse(Visit.objects.exists())

    def test_quick_pick_counts_from_the_visit_date(self, refresh):
        self.client.post(create_url(self.patient), visit_post_data(seen_on=self.PAPER_DAY.isoformat(), follow_up_in="14d"))
        self.assertEqual(Visit.objects.get().follow_up_date, date(2024, 3, 15))

    def test_future_visit_date_is_rejected(self, refresh):
        tomorrow = timezone.localdate() + timedelta(days=1)
        response = self.client.post(create_url(self.patient), visit_post_data(seen_on=tomorrow.isoformat()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["seen_on"], ["The visit date can't be in the future."])
        self.assertFalse(Visit.objects.exists())

    def test_visit_date_before_birth_is_rejected(self, refresh):
        before_birth = self.patient.date_of_birth - timedelta(days=1)
        response = self.client.post(create_url(self.patient), visit_post_data(seen_on=before_birth.isoformat()))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("seen_on"))
        self.assertFalse(Visit.objects.exists())

    def test_back_dated_visit_does_not_close_todays_appointment(self, refresh):
        waiting = self.make_appointment(self.patient, when=timezone.now(), status=Appointment.Status.ARRIVED)
        for url in (create_url(self.patient), create_url(self.patient, appointment=waiting.pk)):
            with self.subTest(url=url):
                Visit.objects.all().delete()
                self.client.post(url, visit_post_data(seen_on=self.PAPER_DAY.isoformat()))
                self.assertIsNone(Visit.objects.get().appointment)
                waiting.refresh_from_db()
                self.assertEqual(waiting.status, Appointment.Status.ARRIVED)  # still in the waiting room

    def test_repeat_and_previous_visit_still_use_the_real_latest_visit(self, refresh):
        latest = self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=5), diagnosis="Real latest")
        self.add_items(latest, "Tab. Latest 5mg")
        data = visit_post_data(seen_on=self.PAPER_DAY.isoformat(), rows=[{"medicine": "Tab. Paper 1mg"}])
        self.client.post(create_url(self.patient), data)

        response = self.client.get(create_url(self.patient))
        self.assertEqual(response.context["previous_visit"], latest)
        self.assertEqual(response.context["last_rx_visit"], latest)
        self.assertContains(response, f"?copy_from={latest.pk}")

    def test_edit_can_move_the_visit_date(self, refresh):
        visit = self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=2))
        original_time = timezone.localtime(visit.visit_date).time()
        response = self.client.get(update_url(visit))
        self.assertContains(response, f'name="seen_on" value="{timezone.localdate(visit.visit_date).isoformat()}"')

        self.client.post(update_url(visit), visit_post_data(seen_on=self.PAPER_DAY.isoformat()))
        visit.refresh_from_db()
        moved = timezone.localtime(visit.visit_date)
        self.assertEqual(moved.date(), self.PAPER_DAY)
        self.assertEqual(moved.time(), original_time)  # same time of day: order within the day is kept

    def test_editing_other_fields_keeps_the_exact_date_and_time(self, refresh):
        when = timezone.make_aware(datetime.combine(timezone.localdate() - timedelta(days=3), time(10, 25, 13)))
        visit = self.make_visit(self.patient, visit_date=when)
        day = timezone.localdate(when).isoformat()
        self.client.post(update_url(visit), visit_post_data(seen_on=day, diagnosis="Changed"))
        visit.refresh_from_db()
        self.assertEqual(visit.diagnosis, "Changed")
        self.assertEqual(visit.visit_date, when)


class BackDatedVisitReminderTests(ClinicalTestCase):
    """Typing in an old paper visit must not cancel the reminders of the patient's real visits."""

    def test_reminders_of_a_later_visit_stay_pending(self):
        today = timezone.localdate()
        real = self.make_visit(
            self.patient,
            visit_date=timezone.now() - timedelta(days=10),
            follow_up_date=today + timedelta(days=self.clinic.followup_reminder_days),
        )
        generate_reminders(self.clinic)
        reminder = Reminder.objects.get(visit=real, kind=Reminder.Kind.FOLLOW_UP)
        self.assertEqual(reminder.status, Reminder.Status.PENDING)

        self.login(self.doctor)
        data = visit_post_data(seen_on="2024-03-01", follow_up_date="2024-03-15", rows=[{"medicine": "Tab. Old 1mg"}])
        response = self.client.post(create_url(self.patient), data)  # the real refresh_for_visit runs
        self.assertEqual(response.status_code, 302)
        paper = Visit.objects.exclude(pk=real.pk).get()
        self.assertEqual(timezone.localdate(paper.visit_date), date(2024, 3, 1))

        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.PENDING)
        generate_reminders(self.clinic)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, Reminder.Status.PENDING)
        # The old visit's long-past follow-up gets no reminder of its own.
        self.assertFalse(Reminder.objects.filter(visit=paper).exists())

        # The patient's profile still treats the real visit as the latest.
        summary = clinical_summary(self.patient)
        self.assertEqual(summary["latest_visit"], real)


class RepeatPrescriptionTests(ClinicalTestCase):
    def setUp(self):
        self.login(self.doctor)

    def make_earlier_visit(self, patient=None, doctor=None, days_ago=30, **kwargs):
        when = timezone.now() - timedelta(days=days_ago)
        return self.make_visit(patient or self.patient, doctor=doctor, visit_date=when, **kwargs)

    def test_repeat_button_shown_when_an_earlier_visit_has_medicines(self):
        earlier = self.make_earlier_visit()
        self.add_items(earlier, "Tab. Amlodipine 5mg")
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, "Repeat last prescription")
        self.assertContains(response, f"?copy_from={earlier.pk}")

    def test_no_repeat_button_without_earlier_medicines(self):
        self.make_earlier_visit()  # a visit, but no medicines
        response = self.client.get(create_url(self.patient))
        self.assertNotContains(response, "Repeat last prescription")

    def test_repeat_link_keeps_the_appointment(self):
        earlier = self.make_earlier_visit()
        self.add_items(earlier, "Tab. Amlodipine 5mg")
        appointment = self.make_appointment(self.patient)
        response = self.client.get(create_url(self.patient, appointment=appointment.pk))
        self.assertContains(response, f"copy_from={earlier.pk}&amp;appointment={appointment.pk}")

    def test_copy_from_fills_the_prescription_rows(self):
        earlier = self.make_earlier_visit()
        self.add_items(earlier, "Tab. Amlodipine 5mg", "Tab. Metformin 500mg")
        response = self.client.get(create_url(self.patient, copy_from=earlier.pk))

        self.assertContains(response, "Medicines copied from the visit on")
        formset = response.context["formset"]
        self.assertEqual(
            [form.initial.get("medicine") for form in formset.forms[:2]],
            ["Tab. Amlodipine 5mg", "Tab. Metformin 500mg"],
        )
        self.assertGreaterEqual(formset.total_form_count(), 3)  # room to add more
        self.assertContains(response, 'value="Tab. Amlodipine 5mg"')
        self.assertNotContains(response, "Repeat last prescription")  # already repeated

    def test_copy_from_another_patient_or_clinic_is_ignored(self):
        other_clinic_visit = self.make_earlier_visit(patient=self.other_patient, doctor=self.other_owner)
        self.add_items(other_clinic_visit, "Tab. Secret 1mg")
        someone_else = make_patient(self.clinic, full_name="Usman Tariq")
        their_visit = self.make_earlier_visit(patient=someone_else)
        self.add_items(their_visit, "Tab. Theirs 2mg")

        for visit in (other_clinic_visit, their_visit):
            with self.subTest(visit=visit.pk):
                response = self.client.get(create_url(self.patient, copy_from=visit.pk))
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["copied_from"])
                initial = [form.initial.get("medicine") for form in response.context["formset"].forms]
                self.assertEqual(initial, [None, None, None])
                self.assertNotContains(response, "Tab. Secret 1mg")  # not even as a suggestion

    def test_previous_visit_card(self):
        earlier = self.make_earlier_visit(diagnosis="Essential hypertension")
        self.add_items(earlier, "Tab. Amlodipine 5mg")
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, "Previous visit")
        self.assertContains(response, "Essential hypertension")
        self.assertContains(response, "<strong>Tab. Amlodipine 5mg</strong>", html=True)
        # Opens in a new tab, so the notes being typed are not lost.
        self.assertContains(
            response, f'<a class="btn btn-ghost btn-sm" href="{earlier.get_absolute_url()}" target="_blank" rel="noopener">Open</a>',
            html=True,
        )

    def test_history_shows_the_last_three_visits(self):
        visits = [
            self.make_earlier_visit(days_ago=days, chief_complaint=f"Complaint {n}", diagnosis=f"Diagnosis {n}")
            for n, days in enumerate([10, 40, 90, 200], start=1)
        ]
        self.add_items(visits[1], "Tab. Second 1mg", "Syp. Second 2ml")
        response = self.client.get(create_url(self.patient))

        self.assertEqual(response.context["previous_visit"], visits[0])  # the newest, in full
        self.assertEqual(response.context["older_visits"], visits[1:3])  # two more, in short
        for n in (1, 2, 3):
            self.assertContains(response, f"Complaint {n}")
            self.assertContains(response, f"Diagnosis {n}")
        self.assertContains(response, "Tab. Second 1mg, Syp. Second 2ml")
        self.assertNotContains(response, "Complaint 4")
        self.assertContains(response, f'href="{visits[2].get_absolute_url()}" target="_blank" rel="noopener"')

    def test_history_never_shows_other_patients_or_clinics(self):
        self.make_earlier_visit(patient=self.other_patient, doctor=self.other_owner, chief_complaint="Elsewhere complaint")
        usman = make_patient(self.clinic, full_name="Usman Tariq")
        self.make_earlier_visit(patient=usman, chief_complaint="Usman complaint")
        LabResult.objects.create(clinic=self.clinic, patient=usman, test_name="Usman lab")
        LabResult.objects.create(clinic=self.other_clinic, patient=self.other_patient, test_name="Elsewhere lab")
        response = self.client.get(create_url(self.patient))
        self.assertIsNone(response.context["previous_visit"])
        self.assertEqual(response.context["recent_labs"], [])
        for text in ("Elsewhere complaint", "Usman complaint", "Usman lab", "Elsewhere lab"):
            self.assertNotContains(response, text)

    def test_edit_page_history_shows_only_earlier_visits(self):
        older = self.make_earlier_visit(days_ago=30, chief_complaint="Came before")
        this_visit = self.make_earlier_visit(days_ago=10)
        self.make_earlier_visit(days_ago=1, chief_complaint="Came back later")
        response = self.client.get(update_url(this_visit))
        self.assertEqual(response.context["previous_visit"], older)
        self.assertContains(response, "Came before")
        self.assertNotContains(response, "Came back later")

    def test_recent_lab_results_and_history_links(self):
        LabResult.objects.create(clinic=self.clinic, patient=self.patient, test_name="HbA1c", is_abnormal=True)
        with_file = LabResult.objects.create(
            clinic=self.clinic, patient=self.patient, test_name="Chest X-ray", file="clinic_1/labs/x.pdf"
        )
        response = self.client.get(create_url(self.patient))
        self.assertContains(response, "Recent lab results")
        self.assertContains(response, "HbA1c")
        self.assertContains(response, "Abnormal")
        lab_url = reverse("clinical:lab_file", args=[with_file.pk])
        self.assertContains(response, f'href="{lab_url}" target="_blank" rel="noopener"')

        profile = reverse("patients:detail", args=[self.patient.pk])
        self.assertContains(response, f'href="{profile}?tab=history" target="_blank" rel="noopener"')
        self.assertContains(response, f'href="{profile}?tab=labs" target="_blank" rel="noopener"')
        self.assertContains(response, 'href="#patient-history"')  # jump link for phones
        self.assertContains(response, 'id="patient-history"')

    def test_no_previous_visit_card_for_a_first_visit(self):
        response = self.client.get(create_url(self.patient))
        self.assertNotContains(response, "Previous visit")
        self.assertNotContains(response, 'href="#patient-history"')

    def test_medicine_suggestions_come_from_this_clinic_only(self):
        earlier = self.make_earlier_visit()
        self.add_items(earlier, "Tab. Metformin 500mg")
        other_visit = self.make_earlier_visit(patient=self.other_patient, doctor=self.other_owner)
        self.add_items(other_visit, "Tab. Other Clinic 1mg")

        response = self.client.get(create_url(self.patient))
        self.assertContains(response, '<option value="Tab. Metformin 500mg"></option>', html=True)
        self.assertNotContains(response, "Tab. Other Clinic 1mg")


@mock.patch(REFRESH_FOR_VISIT)
class VisitUpdateTests(ClinicalTestCase):
    def setUp(self):
        self.visit = self.make_visit(self.patient, doctor=self.doctor, diagnosis="Viral fever")
        self.items = self.add_items(self.visit, "Tab. A", "Tab. B", "Tab. C")

    def test_doctor_edits_own_visit(self, refresh):
        self.login(self.doctor)
        response = self.client.get(update_url(self.visit))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit visit")
        self.assertContains(response, 'name="items-TOTAL_FORMS" value="4"')  # 3 medicines + 1 spare row
        self.assertContains(response, 'name="items-0-DELETE"')
        self.assertContains(response, 'value="Tab. A"')

        data = visit_post_data(
            rows=existing_rows(self.visit), initial_forms=3, chief_complaint="Fever, better now", diagnosis="Dengue"
        )
        response = self.client.post(update_url(self.visit), data)
        self.assertRedirects(response, self.visit.get_absolute_url(), fetch_redirect_response=False)

        self.visit.refresh_from_db()
        self.assertEqual(self.visit.diagnosis, "Dengue")
        self.assertEqual(self.visit.doctor, self.doctor)
        self.assertTrue(self.audit_exists(Action.UPDATE, self.visit, f"Updated visit for {self.patient.mrn}"))
        refresh.assert_called_once_with(self.visit)

    def test_owner_can_edit_and_the_doctor_stays_the_same(self, refresh):
        self.login(self.owner)
        data = visit_post_data(rows=existing_rows(self.visit), initial_forms=3, diagnosis="Typhoid")
        response = self.client.post(update_url(self.visit), data)
        self.assertEqual(response.status_code, 302)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.diagnosis, "Typhoid")
        self.assertEqual(self.visit.doctor, self.doctor)

    def test_another_doctor_cannot_edit(self, refresh):
        self.login(self.second_doctor)
        self.assertEqual(self.client.get(update_url(self.visit)).status_code, 403)
        data = visit_post_data(rows=existing_rows(self.visit), initial_forms=3, diagnosis="Changed")
        self.assertEqual(self.client.post(update_url(self.visit), data).status_code, 403)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.diagnosis, "Viral fever")

    def test_receptionist_cannot_edit(self, refresh):
        self.login(self.receptionist)
        self.assertEqual(self.client.get(update_url(self.visit)).status_code, 403)

    def test_other_clinic_cannot_see_the_visit(self, refresh):
        self.login(self.other_owner)
        self.assertEqual(self.client.get(update_url(self.visit)).status_code, 404)
        self.assertEqual(self.client.post(update_url(self.visit), visit_post_data()).status_code, 404)

    def test_remove_empty_and_add_rows(self, refresh):
        self.login(self.doctor)
        rows = existing_rows(self.visit)
        rows[0]["DELETE"] = "on"  # Tab. A: ticked "Remove"
        rows[1]["medicine"] = ""  # Tab. B: medicine cleared
        rows.append({"medicine": "Tab. D", "dose": "1 tablet"})  # new row
        response = self.client.post(update_url(self.visit), visit_post_data(rows=rows, initial_forms=3))
        self.assertEqual(response.status_code, 302)

        items = list(self.visit.prescription_items.all())
        self.assertEqual([(item.medicine, item.order) for item in items], [("Tab. C", 0), ("Tab. D", 1)])

    def test_medicine_rows_of_another_visit_cannot_be_changed(self, refresh):
        other_visit = self.make_visit(self.other_patient, doctor=self.other_owner)
        (foreign_item,) = self.add_items(other_visit, "Tab. Foreign")
        self.login(self.doctor)

        rows = [{"id": str(foreign_item.pk), "visit": str(self.visit.pk), "medicine": "Hacked"}]
        self.client.post(update_url(self.visit), visit_post_data(rows=rows, initial_forms=1))

        foreign_item.refresh_from_db()
        self.assertEqual(foreign_item.medicine, "Tab. Foreign")
        self.assertEqual(foreign_item.visit, other_visit)
        # The tampered row is skipped, not turned into a new medicine on this visit.
        self.assertFalse(self.visit.prescription_items.filter(medicine="Hacked").exists())

    def test_a_medicine_removed_meanwhile_does_not_block_saving(self, refresh):
        # The edit page was open in two tabs; Tab. B was removed in the other one.
        self.login(self.doctor)
        rows = existing_rows(self.visit)
        self.items[1].delete()
        response = self.client.post(
            update_url(self.visit), visit_post_data(rows=rows, initial_forms=3, diagnosis="Dengue")
        )

        self.assertRedirects(response, self.visit.get_absolute_url(), fetch_redirect_response=False)
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.diagnosis, "Dengue")
        # Not brought back: someone removed it on purpose. The doctor is told.
        self.assertEqual([item.medicine for item in self.visit.prescription_items.all()], ["Tab. A", "Tab. C"])
        self.assertIn("One medicine had already been removed from this visit", " ".join(messages_of(response)))

    def test_saving_the_same_page_twice_works(self, refresh):
        # A double tap on "Save" sends the same form twice; the first one already removed Tab. B.
        self.login(self.doctor)
        rows = existing_rows(self.visit)
        rows[1]["medicine"] = ""
        data = visit_post_data(rows=rows, initial_forms=3, diagnosis="Dengue")
        first = self.client.post(update_url(self.visit), data)
        second = self.client.post(update_url(self.visit), data)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual([item.medicine for item in self.visit.prescription_items.all()], ["Tab. A", "Tab. C"])
        self.assertFalse([m for m in messages_of(second) if "already been removed" in m])  # it was meant to go

    def test_errors_on_hidden_row_fields_are_shown(self, refresh):
        # A row that names another visit fails on its hidden "visit" field: the error must be visible.
        other_visit = self.make_visit(self.patient)
        self.login(self.doctor)
        rows = existing_rows(self.visit)
        rows[0]["visit"] = str(other_visit.pk)
        response = self.client.post(update_url(self.visit), visit_post_data(rows=rows, initial_forms=3))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The visit was not saved")
        self.assertContains(response, '<div class="rx-row-errors"><ul class="errorlist', html=False)

    def test_editing_never_touches_appointments(self, refresh):
        appointment = self.make_appointment(self.patient)
        self.login(self.doctor)
        url = update_url(self.visit) + f"?appointment={appointment.pk}"
        self.client.post(url, visit_post_data(rows=existing_rows(self.visit), initial_forms=3))
        self.visit.refresh_from_db()
        appointment.refresh_from_db()
        self.assertIsNone(self.visit.appointment)
        self.assertEqual(appointment.status, Appointment.Status.SCHEDULED)


class DoubleSaveTests(ClinicalTestCase):
    """Save pressed twice (or the browser sending the form again) records the visit once."""

    def setUp(self):
        self.login(self.doctor)

    def form_token(self):
        return self.client.get(create_url(self.patient)).context["form_token"]

    def test_the_same_form_sent_twice_saves_one_visit(self):
        data = visit_post_data(rows=[PARACETAMOL], form_token=self.form_token())
        first = self.client.post(create_url(self.patient), data)
        visit = Visit.objects.get()
        self.assertRedirects(first, visit.get_absolute_url(), fetch_redirect_response=False)

        second = self.client.post(create_url(self.patient), data)
        self.assertRedirects(second, visit.get_absolute_url(), fetch_redirect_response=False)
        self.assertIn("This visit was already saved.", messages_of(second))
        self.assertEqual(Visit.objects.count(), 1)
        self.assertEqual(PrescriptionItem.objects.count(), 1)
        self.assertEqual(AuditLog.objects.filter(action=Action.CREATE).count(), 1)

    def test_each_new_form_records_its_own_visit(self):
        for _ in range(2):
            self.client.post(create_url(self.patient), visit_post_data(form_token=self.form_token()))
        self.assertEqual(Visit.objects.count(), 2)

    def test_a_form_shown_again_with_errors_keeps_its_token(self):
        token = self.form_token()
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.post(create_url(self.patient), visit_post_data(seen_on=tomorrow, form_token=token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form_token"], token)
        self.assertContains(response, f'name="form_token" value="{token}"')

    def test_a_made_up_token_is_replaced(self):
        response = self.client.post(
            create_url(self.patient), visit_post_data(seen_on="2999-01-01", form_token="<script>")
        )
        self.assertRegex(response.context["form_token"], r"^[0-9a-f]{32}$")

    def test_the_edit_form_has_no_token(self):
        visit = self.make_visit(self.patient)
        response = self.client.get(update_url(visit))
        self.assertNotContains(response, 'name="form_token"')
