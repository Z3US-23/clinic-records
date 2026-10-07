"""Day and week schedule pages."""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.reminders import services as reminder_services
from apps.reminders.models import Reminder

from .base import AppointmentTestCase

Status = Appointment.Status


class DayViewTests(AppointmentTestCase):
    def test_renders_for_every_role(self):
        self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.get(self.day_url(self.tomorrow))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "appointments/day.html")
                self.assertContains(response, "Ayesha Khan Baloch")

    def test_requires_login(self):
        response = self.client.get(reverse("appointments:day"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_defaults_to_clinic_today(self):
        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:day"))
        self.assertEqual(response.context["day"], self.today)
        self.assertTrue(response.context["is_today"])

    def test_invalid_or_out_of_range_date_falls_back_to_today(self):
        self.login(self.receptionist)
        for raw in ("not-a-date", "2026-13-45", "9999-12-31", "0001-01-01", ""):
            with self.subTest(raw=raw):
                response = self.client.get(reverse("appointments:day"), {"date": raw})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["day"], self.today)

    def test_only_shows_this_clinics_appointments_for_that_day(self):
        self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        self.make_appointment(self.patient, when=self.at(self.tomorrow + timedelta(days=1), 10))
        other_patient = self.make_patient(clinic=self.other_clinic, full_name="Zainab Other")
        self.make_appointment(other_patient, doctor=self.other_owner, when=self.at(self.tomorrow, 10))

        self.login(self.doctor)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertEqual(len(response.context["schedule"]), 1)
        self.assertNotContains(response, "Zainab Other")

    def test_doctor_filter(self):
        self.make_appointment(self.patient, doctor=self.doctor, when=self.at(self.tomorrow, 10))
        other = self.make_patient(full_name="Imran Sheikh")
        self.make_appointment(other, doctor=self.owner, when=self.at(self.tomorrow, 11))
        self.login(self.receptionist)

        response = self.client.get(self.day_url(self.tomorrow, doctor=self.doctor.pk))
        self.assertEqual(response.context["selected_doctor"], self.doctor)
        self.assertEqual([a.patient for a in response.context["schedule"]], [self.patient])

    def test_doctor_filter_ignores_anything_that_is_not_a_clinic_doctor(self):
        self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        self.login(self.receptionist)
        for raw in ("abc", "99999", "-1", "1.5", str(self.receptionist.pk), str(self.other_owner.pk)):
            with self.subTest(doctor=raw):
                response = self.client.get(self.day_url(self.tomorrow, doctor=raw))
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["selected_doctor"])
                self.assertEqual(len(response.context["schedule"]), 1)

    def test_status_counts(self):
        for hour, status in [(9, Status.SCHEDULED), (10, Status.SCHEDULED), (11, Status.CONFIRMED),
                             (12, Status.NO_SHOW), (13, Status.CANCELLED)]:
            self.make_appointment(self.patient, when=self.at(self.tomorrow, hour), status=status)
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        counts = {chip["status"]: chip["count"] for chip in response.context["status_chips"]}
        self.assertEqual(counts[Status.SCHEDULED], 2)
        self.assertEqual(counts[Status.CONFIRMED], 1)
        self.assertEqual(counts[Status.NO_SHOW], 1)
        self.assertEqual(counts[Status.CANCELLED], 1)
        self.assertEqual(counts[Status.ARRIVED], 0)
        self.assertEqual(response.context["active_count"], 4)
        for label in ("Booked", "Confirmed", "Wants another time", "Waiting", "Seen", "Did not come", "Cancelled"):
            self.assertContains(response, label)

    def test_cancelled_appointments_are_listed_last(self):
        cancelled = self.make_appointment(self.patient, when=self.at(self.tomorrow, 9), status=Status.CANCELLED)
        later = self.make_appointment(self.patient, when=self.at(self.tomorrow, 15))
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertEqual(response.context["schedule"], [later, cancelled])
        self.assertContains(response, "is-cancelled")

    def test_patient_note_shown_when_patient_wants_another_time(self):
        self.make_appointment(
            self.patient, when=self.at(self.tomorrow, 10),
            status=Status.RESCHEDULE_REQUESTED, patient_note="Friday evening please",
        )
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertContains(response, "Friday evening please")
        self.assertContains(response, "Wants another time")

    def test_reminder_state(self):
        sent = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        pending = self.make_appointment(self.patient, when=self.at(self.tomorrow, 11))
        not_yet = self.make_appointment(self.patient, when=self.at(self.tomorrow, 12))
        cancelled = self.make_appointment(self.patient, when=self.at(self.tomorrow, 13), status=Status.CANCELLED)
        for appointment, status in [(sent, Reminder.Status.SENT), (pending, Reminder.Status.PENDING)]:
            Reminder.objects.create(
                clinic=self.clinic, patient=self.patient, appointment=appointment,
                kind=Reminder.Kind.APPOINTMENT, due_date=self.today, message="Reminder", status=status,
            )
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertContains(response, "Sent")
        self.assertContains(response, "To send")
        self.assertContains(response, "Not yet", count=1)
        rows = {a.pk: a for a in response.context["schedule"]}
        self.assertTrue(rows[not_yet.pk].reminder_expected)
        self.assertFalse(rows[cancelled.pk].reminder_expected)  # shown as "—", not "Not yet"
        self.assertNotContains(response, "Reminders off")

    def test_reminders_off_for_patients_who_opted_out_or_were_archived(self):
        self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        opted_out = self.make_patient(full_name="Opted Out", reminders_opt_in=False)
        self.make_appointment(opted_out, when=self.at(self.tomorrow, 11))
        archived = self.make_patient(full_name="Archived Later")
        self.make_appointment(archived, when=self.at(self.tomorrow, 12))
        archived.is_archived = True  # archived after the appointment was booked
        archived.save()
        # Cancelled: no reminder is expected anyway, so it stays "—".
        self.make_appointment(opted_out, when=self.at(self.tomorrow, 13), status=Status.CANCELLED)

        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertContains(response, "Not yet", count=1)  # only the patient who wants reminders
        self.assertContains(response, "Reminders off", count=2)
        self.assertContains(response, 'title="Patient did not agree to WhatsApp reminders"')
        self.assertContains(response, 'title="Patient is archived"')
        rows = {a.patient.full_name: a for a in response.context["schedule"] if a.status != Status.CANCELLED}
        self.assertFalse(rows["Opted Out"].reminder_expected)
        self.assertTrue(rows["Opted Out"].reminders_off)

    def test_an_earlier_reminder_stays_visible_after_opting_out(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        Reminder.objects.create(
            clinic=self.clinic, patient=self.patient, appointment=appointment, kind=Reminder.Kind.APPOINTMENT,
            due_date=self.today, message="Reminder", status=Reminder.Status.SENT,
        )
        self.patient.reminders_opt_in = False
        self.patient.save()

        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertContains(response, "Sent")
        self.assertNotContains(response, "Reminders off")
        self.assertNotContains(response, "Not yet")

    def test_reminder_column_agrees_with_the_reminder_service(self):
        """The row says "Reminders off" exactly when the service prepares none (a missing number does not count)."""
        patients = [
            self.patient,
            self.make_patient(full_name="Opted Out", reminders_opt_in=False),
            self.make_patient(full_name="Archived", is_archived=True),
            self.make_patient(full_name="No Mobile", phone=""),
        ]
        for hour, patient in enumerate(patients, start=10):
            self.make_appointment(patient, when=self.at(self.tomorrow, hour))
        reminder_services.generate_reminders(self.clinic)

        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        for row in response.context["schedule"]:
            with self.subTest(patient=row.patient.full_name):
                self.assertEqual(row.reminders_off, row.reminder is None)
        self.assertContains(response, "Reminders off", count=2)

    def test_quick_actions_match_the_status(self):
        booked = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        seen = self.make_appointment(self.patient, when=self.at(self.tomorrow, 11), status=Status.COMPLETED)
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        html = response.content.decode()
        booked_url = reverse("appointments:set_status", args=[booked.pk])
        self.assertIn(f'action="{booked_url}"', html)
        self.assertContains(response, 'data-confirm="Cancel this appointment?"')
        self.assertNotIn(f'action="{reverse("appointments:set_status", args=[seen.pk])}"', html)
        self.assertContains(response, reverse("appointments:update", args=[seen.pk]))

    def test_waiting_room_lists_arrived_patients_in_arrival_order(self):
        first = self.make_patient(full_name="First Arrival")
        second = self.make_patient(full_name="Second Arrival")
        now = timezone.now()
        late = self.make_appointment(second, when=now, status=Status.ARRIVED)
        early = self.make_appointment(first, when=now, status=Status.ARRIVED)
        Appointment.objects.filter(pk=early.pk).update(arrived_at=now - timedelta(minutes=20))
        Appointment.objects.filter(pk=late.pk).update(arrived_at=now - timedelta(minutes=5))

        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:day"))
        self.assertEqual([a.pk for a in response.context["waiting"]], [early.pk, late.pk])
        self.assertContains(response, "Waiting room")
        self.assertContains(response, "Mark seen")
        self.assertContains(response, 'title="Token 1"')
        self.assertContains(response, 'title="Token 2"')

    def test_start_visit_only_for_clinicians(self):
        appointment = self.make_appointment(self.patient, when=timezone.now(), status=Status.ARRIVED)
        visit_url = reverse("clinical:visit_create", args=[self.patient.pk]) + f"?appointment={appointment.pk}"

        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:day"))
        self.assertNotContains(response, "Start visit")

        for user in (self.doctor, self.owner):
            self.login(user)
            response = self.client.get(reverse("appointments:day"))
            self.assertContains(response, "Start visit")
            self.assertContains(response, visit_url)

    def test_empty_state(self):
        self.login(self.receptionist)
        response = self.client.get(self.day_url(self.tomorrow))
        self.assertContains(response, "No appointments on this day")
        self.assertContains(response, f"{reverse('appointments:create')}?date={self.tomorrow.isoformat()}")

    def test_number_of_queries_does_not_grow_with_appointments(self):
        self.login(self.receptionist)

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(self.day_url(self.tomorrow))
            return len(ctx.captured_queries)

        self.make_appointment(self.patient, when=self.at(self.tomorrow, 9))
        baseline = count_queries()
        for hour in range(10, 16):
            patient = self.make_patient(full_name=f"Patient {hour}")
            appointment = self.make_appointment(patient, when=self.at(self.tomorrow, hour))
            Reminder.objects.create(
                clinic=self.clinic, patient=patient, appointment=appointment,
                kind=Reminder.Kind.APPOINTMENT, due_date=self.today, message="Reminder",
            )
        self.assertEqual(count_queries(), baseline)


class WeekViewTests(AppointmentTestCase):
    def test_renders_for_every_role(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.email):
                self.login(user)
                response = self.client.get(reverse("appointments:week"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "appointments/week.html")
                self.assertEqual(len(response.context["days"]), 7)

    def test_defaults_to_monday_of_this_week(self):
        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:week"))
        monday = self.today - timedelta(days=self.today.weekday())
        self.assertEqual(response.context["week_start"], monday)

    def test_invalid_start_falls_back_to_this_week(self):
        self.login(self.receptionist)
        monday = self.today - timedelta(days=self.today.weekday())
        for raw in ("rubbish", "2026-02-30", "99999-01-01"):
            with self.subTest(raw=raw):
                response = self.client.get(reverse("appointments:week"), {"start": raw})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["week_start"], monday)

    def test_start_snaps_to_monday(self):
        self.login(self.receptionist)
        response = self.client.get(reverse("appointments:week"), {"start": "2026-10-08"})  # a Thursday
        self.assertEqual(response.context["week_start"].isoformat(), "2026-10-05")
        self.assertEqual(response.context["prev_week"].isoformat(), "2026-09-28")
        self.assertEqual(response.context["next_week"].isoformat(), "2026-10-12")

    def test_appointments_grouped_by_clinic_day_and_isolated(self):
        monday = self.tomorrow - timedelta(days=self.tomorrow.weekday())
        wednesday = monday + timedelta(days=2)
        # 00:30 local on Wednesday is still Tuesday in UTC: must land on Wednesday.
        early = self.make_appointment(self.patient, when=self.at(wednesday, 0, 30))
        cancelled = self.make_appointment(self.patient, when=self.at(wednesday, 9), status=Status.CANCELLED)
        other_patient = self.make_patient(clinic=self.other_clinic, full_name="Zainab Other")
        self.make_appointment(other_patient, doctor=self.other_owner, when=self.at(wednesday, 10))

        self.login(self.doctor)
        response = self.client.get(reverse("appointments:week"), {"start": monday.isoformat()})
        day = response.context["days"][2]
        self.assertEqual(day["date"], wednesday)
        self.assertEqual(day["appointments"], [early, cancelled])
        self.assertEqual(day["count"], 1)  # cancelled ones are not counted
        self.assertNotContains(response, "Zainab Other")
        self.assertContains(response, f"?date={wednesday.isoformat()}")


class AccessTests(AppointmentTestCase):
    """Every appointment page needs a signed-in user who works in a clinic (any role)."""

    def staff_urls(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.tomorrow, 10))
        return [
            reverse("appointments:day"),
            reverse("appointments:week"),
            reverse("appointments:create"),
            reverse("appointments:create") + f"?patient={self.patient.pk}",
            reverse("appointments:update", args=[appointment.pk]),
        ]

    def test_signed_out_users_are_sent_to_login(self):
        for url in self.staff_urls():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("accounts:login"), response["Location"])

    def test_users_without_a_clinic_are_sent_away(self):
        self.receptionist.memberships.update(is_active=False)
        self.login(self.receptionist)
        for url in self.staff_urls():
            with self.subTest(url=url):
                self.assertRedirects(self.client.get(url), reverse("accounts:no_clinic"), fetch_redirect_response=False)
