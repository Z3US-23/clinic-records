"""Booking (create) and editing (update) appointments."""

from datetime import timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format, time_format

from apps.appointments.models import Appointment
from apps.core.models import AuditLog

from .base import KARACHI, AppointmentTestCase

Status = Appointment.Status
REFRESH = "apps.reminders.services.refresh_for_appointment"


class PatientPickerTests(AppointmentTestCase):
    url = reverse("appointments:create")

    def test_renders_for_every_role(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "appointments/patient_picker.html")
                self.assertContains(response, reverse("patients:create"))

    def test_search_by_name_mrn_and_phone(self):
        self.login(self.receptionist)
        for query in ("ayesha", self.patient.mrn, "0300-1234567", "03001234567", "300 1234567"):
            with self.subTest(query=query):
                response = self.client.get(self.url, {"q": query})
                self.assertIn(self.patient, list(response.context["patients"]))

    def test_search_is_limited_to_this_clinic_and_skips_archived(self):
        other = self.make_patient(clinic=self.other_clinic, full_name="Ayesha Other Clinic")
        archived = self.make_patient(full_name="Ayesha Archived", is_archived=True)
        self.login(self.receptionist)
        response = self.client.get(self.url, {"q": "Ayesha"})
        patients = list(response.context["patients"])
        self.assertIn(self.patient, patients)
        self.assertNotIn(other, patients)
        self.assertNotIn(archived, patients)

    def test_choose_link_keeps_the_date(self):
        self.login(self.receptionist)
        response = self.client.get(self.url, {"q": "Ayesha", "date": self.tomorrow.isoformat()})
        self.assertContains(response, f"date={self.tomorrow.isoformat()}&amp;patient={self.patient.pk}")

    def test_no_results(self):
        self.login(self.receptionist)
        response = self.client.get(self.url, {"q": "Nobody By This Name"})
        self.assertContains(response, "No patient found")


class CreateAppointmentTests(AppointmentTestCase):
    def url(self, patient=None, **params):
        query = {"patient": (patient or self.patient).pk, **params}
        return reverse("appointments:create") + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def post_data(self, **overrides):
        data = {
            "date": self.tomorrow.isoformat(),
            "time": "10:30",
            "doctor": self.doctor.pk,
            "duration_minutes": 15,
            "reason": "BP check",
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def test_form_renders_for_every_role_with_defaults(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.get(self.url(date=self.tomorrow.isoformat()))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "appointments/form.html")
                form = response.context["form"]
                self.assertEqual(form["date"].value(), self.tomorrow)
                self.assertEqual(form["duration_minutes"].value(), self.clinic.default_appointment_minutes)
                self.assertContains(response, "Any doctor")
                self.assertContains(response, "Dr. Bilal Hussain")
                self.assertContains(response, "Patient is here now")

    def test_doctor_choices_are_only_this_clinics_doctors(self):
        self.login(self.receptionist)
        response = self.client.get(self.url())
        doctors = set(response.context["form"].fields["doctor"].queryset)
        self.assertEqual(doctors, {self.owner, self.doctor})

    def test_patient_must_belong_to_this_clinic_and_not_be_archived(self):
        other = self.make_patient(clinic=self.other_clinic)
        archived = self.make_patient(full_name="Old Patient", is_archived=True)
        self.login(self.owner)
        for patient_id in (other.pk, archived.pk, 999999, "abc"):
            with self.subTest(patient=patient_id):
                url = reverse("appointments:create") + f"?patient={patient_id}"
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, self.post_data()).status_code, 404)
        self.assertFalse(Appointment.objects.exists())

    @mock.patch(REFRESH)
    def test_book_appointment(self, refresh):
        self.login(self.receptionist)
        response = self.client.post(self.url(), self.post_data(), follow=True)

        appointment = Appointment.objects.get()
        when = self.at(self.tomorrow, 10, 30)
        self.assertEqual(appointment.scheduled_at, when)
        self.assertEqual(appointment.clinic, self.clinic)
        self.assertEqual(appointment.patient, self.patient)
        self.assertEqual(appointment.doctor, self.doctor)
        self.assertEqual(appointment.created_by, self.receptionist)
        self.assertEqual(appointment.status, Status.SCHEDULED)
        self.assertEqual(appointment.reason, "BP check")

        self.assertRedirects(response, self.day_url(self.tomorrow))
        local = timezone.localtime(when, KARACHI)
        self.assertContains(
            response,
            f"Appointment booked for Ayesha Khan Baloch on {date_format(local, 'j M Y')} at {time_format(local, 'g:i a')}.",
        )
        refresh.assert_called_once_with(appointment)
        log = self.audit_entries(appointment, AuditLog.Action.CREATE).get()
        self.assertEqual(log.summary, f"Booked appointment for {self.patient.mrn}")
        self.assertEqual(log.user, self.receptionist)
        self.assertEqual(log.clinic, self.clinic)

    def test_time_is_read_in_the_clinic_timezone(self):
        self.clinic.timezone = "Asia/Kolkata"
        self.clinic.save()
        self.login(self.owner)
        self.client.post(self.url(), self.post_data(time="09:00"))
        appointment = Appointment.objects.get()
        # 09:00 in India is 03:30 UTC
        self.assertEqual(appointment.scheduled_at.astimezone(dt_timezone.utc).strftime("%H:%M"), "03:30")

    def test_any_doctor(self):
        self.login(self.receptionist)
        self.client.post(self.url(), self.post_data(doctor=""))
        self.assertIsNone(Appointment.objects.get().doctor)

    @mock.patch(REFRESH)
    def test_walk_in_goes_straight_to_the_waiting_room(self, refresh):
        self.login(self.receptionist)
        before = timezone.now()
        response = self.client.post(self.url(), self.post_data(walk_in="on", date="", time=""))
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.status, Status.ARRIVED)
        self.assertTrue(before <= appointment.scheduled_at <= timezone.now())
        self.assertRedirects(response, self.day_url(timezone.localtime(appointment.scheduled_at, KARACHI).date()))
        refresh.assert_called_once()

    def test_walk_in_skips_the_double_booking_check(self):
        self.make_appointment(self.make_patient(full_name="Booked Earlier"), when=timezone.now())
        self.login(self.receptionist)
        self.client.post(self.url(), self.post_data(walk_in="on"))
        self.assertEqual(Appointment.objects.filter(status=Status.ARRIVED).count(), 1)

    def test_date_and_time_required_unless_walk_in(self):
        self.login(self.receptionist)
        response = self.client.post(self.url(), self.post_data(date="", time=""))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("date", form.errors)
        self.assertIn("time", form.errors)
        self.assertFalse(Appointment.objects.exists())

    def test_rejects_times_in_the_past(self):
        self.login(self.receptionist)
        yesterday = self.today - timedelta(days=1)
        response = self.client.post(self.url(), self.post_data(date=yesterday.isoformat()))
        self.assertIn("already passed", str(response.context["form"].errors["time"]))
        self.assertFalse(Appointment.objects.exists())

    def test_a_few_minutes_ago_is_still_accepted(self):
        two_minutes_ago = timezone.localtime(timezone.now() - timedelta(minutes=2), KARACHI)
        self.login(self.receptionist)
        self.client.post(self.url(), self.post_data(
            date=two_minutes_ago.date().isoformat(), time=two_minutes_ago.strftime("%H:%M"),
        ))
        self.assertTrue(Appointment.objects.exists())

    def test_rejects_dates_years_ahead(self):
        self.login(self.receptionist)
        far = self.today.replace(year=self.today.year + 30)
        response = self.client.post(self.url(), self.post_data(date=far.isoformat()))
        self.assertIn("date", response.context["form"].errors)

    def test_rejects_a_doctor_from_another_clinic_or_a_receptionist(self):
        self.login(self.owner)
        for doctor in (self.other_owner, self.receptionist):
            with self.subTest(doctor=doctor.email):
                response = self.client.post(self.url(), self.post_data(doctor=doctor.pk))
                self.assertEqual(response.status_code, 200)
                self.assertIn("doctor", response.context["form"].errors)
        self.assertFalse(Appointment.objects.exists())

    def test_duration_limits(self):
        self.login(self.receptionist)
        for minutes in (0, 2, 10_000, "abc"):
            with self.subTest(minutes=minutes):
                response = self.client.post(self.url(), self.post_data(duration_minutes=minutes))
                self.assertIn("duration_minutes", response.context["form"].errors)

    def test_double_booking_is_refused_then_allowed_when_ticked(self):
        other = self.make_patient(full_name="Imran Sheikh")
        self.make_appointment(other, doctor=self.doctor, when=self.at(self.tomorrow, 10, 0), duration_minutes=15)
        self.login(self.receptionist)

        response = self.client.post(self.url(), self.post_data(time="10:10"))
        self.assertEqual(response.status_code, 200)
        errors = " ".join(response.context["form"].non_field_errors())
        self.assertIn("Imran Sheikh", errors)
        self.assertIn("Dr. Bilal Hussain", errors)
        self.assertContains(response, "Book anyway (double booking)")
        self.assertEqual(Appointment.objects.count(), 1)

        response = self.client.post(self.url(), self.post_data(time="10:10", allow_double_booking="on"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 2)

    def test_no_clash_when_touching_other_doctor_cancelled_or_any_doctor(self):
        other = self.make_patient(full_name="Imran Sheikh")
        self.make_appointment(other, doctor=self.doctor, when=self.at(self.tomorrow, 10, 0), duration_minutes=15)
        self.make_appointment(other, doctor=self.doctor, when=self.at(self.tomorrow, 12, 0), status=Status.CANCELLED)
        self.login(self.receptionist)
        cases = [
            {"time": "10:15"},                       # starts when the other ends
            {"time": "09:45"},                       # ends when the other starts
            {"time": "10:05", "doctor": self.owner.pk},  # a different doctor
            {"time": "12:00"},                       # the 12:00 one is cancelled
            {"time": "10:05", "doctor": ""},         # "any doctor"
        ]
        for case in cases:
            with self.subTest(case=case):
                response = self.client.post(self.url(), self.post_data(**case))
                self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)

    def test_change_patient_link(self):
        self.login(self.receptionist)
        response = self.client.get(self.url(date=self.tomorrow.isoformat()))
        self.assertContains(response, "Change patient")


class UpdateAppointmentTests(AppointmentTestCase):
    def setUp(self):
        super().setUp()
        self.appointment = self.make_appointment(
            self.patient, when=self.at(self.tomorrow, 10), status=Status.CONFIRMED, reason="Fever",
            patient_responded_at=timezone.now(),
        )
        self.url = reverse("appointments:update", args=[self.appointment.pk])

    def post_data(self, **overrides):
        data = {
            "date": self.tomorrow.isoformat(),
            "time": "10:00",
            "doctor": self.doctor.pk,
            "duration_minutes": 15,
            "reason": "Fever",
            "status": Status.CONFIRMED,
        }
        data.update(overrides)
        return data

    def test_renders_for_every_role(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertEqual(form["date"].value(), self.tomorrow)
                self.assertEqual(form["time"].value().strftime("%H:%M"), "10:00")
                self.assertNotIn("walk_in", form.fields)

    def test_other_clinic_gets_404(self):
        self.login(self.other_owner)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.post_data(reason="Hacked")).status_code, 404)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.reason, "Fever")

    @mock.patch(REFRESH)
    def test_editing_details_only_keeps_status_and_time(self, refresh):
        self.login(self.receptionist)
        response = self.client.post(self.url, self.post_data(reason="Fever and cough"))
        self.assertEqual(response.status_code, 302)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.reason, "Fever and cough")
        self.assertEqual(self.appointment.status, Status.CONFIRMED)
        self.assertIsNotNone(self.appointment.patient_responded_at)
        refresh.assert_called_once_with(self.appointment, rescheduled=False)
        log = self.audit_entries(self.appointment, AuditLog.Action.UPDATE).get()
        self.assertEqual(log.summary, f"Updated appointment for {self.patient.mrn}")

    @mock.patch(REFRESH)
    def test_moving_the_time_resets_confirmation(self, refresh):
        self.login(self.receptionist)
        response = self.client.post(self.url, self.post_data(time="11:30"), follow=True)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.scheduled_at, self.at(self.tomorrow, 11, 30))
        self.assertEqual(self.appointment.status, Status.SCHEDULED)
        self.assertIsNone(self.appointment.patient_responded_at)
        refresh.assert_called_once_with(self.appointment, rescheduled=True)
        self.assertContains(response, "Appointment moved to")
        log = self.audit_entries(self.appointment, AuditLog.Action.UPDATE).get()
        self.assertEqual(log.summary, f"Rescheduled appointment for {self.patient.mrn}")

    @mock.patch(REFRESH)
    def test_changing_doctor_counts_as_rescheduled(self, refresh):
        Appointment.objects.filter(pk=self.appointment.pk).update(status=Status.RESCHEDULE_REQUESTED)
        self.login(self.receptionist)
        self.client.post(self.url, self.post_data(doctor=self.owner.pk, status=Status.RESCHEDULE_REQUESTED))
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.doctor, self.owner)
        self.assertEqual(self.appointment.status, Status.SCHEDULED)
        refresh.assert_called_once_with(self.appointment, rescheduled=True)

    def test_past_appointment_can_still_be_edited(self):
        walk_in = self.make_appointment(
            self.patient, when=timezone.now() - timedelta(days=2, minutes=3, seconds=17), status=Status.COMPLETED
        )
        local = timezone.localtime(walk_in.scheduled_at, KARACHI)
        self.login(self.doctor)
        response = self.client.post(reverse("appointments:update", args=[walk_in.pk]), self.post_data(
            date=local.date().isoformat(), time=local.strftime("%H:%M"), reason="Seen for fever",
            status=Status.COMPLETED,
        ))
        self.assertEqual(response.status_code, 302)
        walk_in.refresh_from_db()
        self.assertEqual(walk_in.reason, "Seen for fever")
        self.assertEqual(walk_in.scheduled_at, local)  # seconds kept

    def test_moving_into_the_past_is_refused(self):
        self.login(self.receptionist)
        yesterday = self.today - timedelta(days=1)
        response = self.client.post(self.url, self.post_data(date=yesterday.isoformat()))
        self.assertIn("time", response.context["form"].errors)

    def test_time_of_an_appointment_that_already_happened_can_be_corrected(self):
        seen = self.make_appointment(
            self.patient, when=self.at(self.today - timedelta(days=1), 11), status=Status.COMPLETED
        )
        self.login(self.doctor)
        yesterday = self.today - timedelta(days=1)
        response = self.client.post(reverse("appointments:update", args=[seen.pk]), self.post_data(
            date=yesterday.isoformat(), time="10:15", status=Status.COMPLETED,
        ))
        self.assertEqual(response.status_code, 302)
        seen.refresh_from_db()
        self.assertEqual(seen.scheduled_at, self.at(yesterday, 10, 15))

        # Back to the waiting room is fine; back to "Booked" at a time that has passed is not.
        response = self.client.post(reverse("appointments:update", args=[seen.pk]), self.post_data(
            date=yesterday.isoformat(), time="10:30", status=Status.ARRIVED,
        ))
        self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("appointments:update", args=[seen.pk]), self.post_data(
            date=yesterday.isoformat(), time="10:45", status=Status.SCHEDULED,
        ))
        self.assertIn("time", response.context["form"].errors)

    def test_status_choices_follow_the_allowed_changes(self):
        cancelled = self.make_appointment(self.patient, when=self.at(self.tomorrow, 15), status=Status.CANCELLED)
        url = reverse("appointments:update", args=[cancelled.pk])
        self.login(self.receptionist)
        response = self.client.get(url)
        self.assertEqual([v for v, _ in response.context["form"].fields["status"].choices], [Status.CANCELLED])

        response = self.client.post(url, self.post_data(time="15:00", status=Status.ARRIVED))
        self.assertIn("status", response.context["form"].errors)
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, Status.CANCELLED)

    def test_status_change_via_edit(self):
        self.login(self.receptionist)
        self.client.post(self.url, self.post_data(status=Status.CANCELLED))
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Status.CANCELLED)

    def test_rescheduling_checks_double_booking(self):
        other = self.make_patient(full_name="Imran Sheikh")
        self.make_appointment(other, doctor=self.doctor, when=self.at(self.tomorrow, 11))
        self.login(self.receptionist)
        response = self.client.post(self.url, self.post_data(time="11:05"))
        self.assertIn("Imran Sheikh", " ".join(response.context["form"].non_field_errors()))

    def test_redirects_to_safe_next_only(self):
        self.login(self.receptionist)
        safe = self.day_url(self.tomorrow, doctor=self.doctor.pk)
        response = self.client.post(self.url, {**self.post_data(), "next": safe})
        self.assertRedirects(response, safe, fetch_redirect_response=False)

        response = self.client.post(self.url, {**self.post_data(), "next": "https://evil.example/"})
        self.assertRedirects(response, self.day_url(self.tomorrow), fetch_redirect_response=False)

    def test_without_next_goes_to_the_new_day(self):
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="next"')

        day_after = self.tomorrow + timedelta(days=1)
        response = self.client.post(self.url, self.post_data(date=day_after.isoformat()))
        self.assertRedirects(response, self.day_url(day_after), fetch_redirect_response=False)

    def test_doctor_who_left_stays_selectable(self):
        self.doctor.memberships.update(is_active=False)
        self.login(self.owner)
        response = self.client.post(self.url, self.post_data(reason="Still with the same doctor"))
        self.assertEqual(response.status_code, 302)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.doctor, self.doctor)
