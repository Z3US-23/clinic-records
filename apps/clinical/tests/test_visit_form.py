"""New visit / edit visit: access rules, saving, prescription rows, appointments, follow-up."""

from datetime import timedelta
from unittest import mock

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.clinical.models import PrescriptionItem, Visit
from apps.clinical.utils import add_months
from apps.core.audit import Action
from apps.core.models import AuditLog
from apps.core.testing import make_patient

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


class VisitCreateAccessTests(ClinicalTestCase):
    def test_clinicians_see_the_form(self):
        for user in (self.owner, self.doctor):
            with self.subTest(user=user.full_name):
                self.login(user)
                response = self.client.get(create_url(self.patient))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "clinical/visit_form.html")
                self.assertContains(response, "Ayesha Khan")
                self.assertContains(response, "Allergies: Penicillin")  # red allergy banner
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

    def test_no_previous_visit_card_for_a_first_visit(self):
        response = self.client.get(create_url(self.patient))
        self.assertNotContains(response, "Previous visit")

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

    def test_editing_never_touches_appointments(self, refresh):
        appointment = self.make_appointment(self.patient)
        self.login(self.doctor)
        url = update_url(self.visit) + f"?appointment={appointment.pk}"
        self.client.post(url, visit_post_data(rows=existing_rows(self.visit), initial_forms=3))
        self.visit.refresh_from_db()
        appointment.refresh_from_db()
        self.assertIsNone(self.visit.appointment)
        self.assertEqual(appointment.status, Appointment.Status.SCHEDULED)
