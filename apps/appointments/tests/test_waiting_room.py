"""Waiting room: arrival time, first-come-first-served order and token numbers."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import time_format

from apps.appointments import scheduling
from apps.appointments.models import Appointment

from .base import KARACHI, AppointmentTestCase

Status = Appointment.Status


class ArrivedAtRulesTests(AppointmentTestCase):
    """Appointment.save() keeps `arrived_at` in step with the status, whichever page saves it."""

    def setUp(self):
        super().setUp()
        self.appointment = self.make_appointment(self.patient, when=self.at(self.today, 9))
        self.earlier = timezone.now() - timedelta(minutes=30)

    def set_status(self, status, update_fields=None):
        """Change the status the way the views do, then reload from the database."""
        self.appointment.status = status
        self.appointment.save(update_fields=update_fields)
        self.appointment.refresh_from_db()

    def arrive_earlier(self):
        """The patient arrived half an hour ago."""
        self.set_status(Status.ARRIVED)
        Appointment.objects.filter(pk=self.appointment.pk).update(arrived_at=self.earlier)
        self.appointment.refresh_from_db()

    def test_booked_appointment_has_no_arrival_time(self):
        self.assertIsNone(self.appointment.arrived_at)

    def test_arriving_sets_the_time_once(self):
        before = timezone.now()
        self.set_status(Status.ARRIVED)
        self.assertTrue(before <= self.appointment.arrived_at <= timezone.now())

        self.arrive_earlier()
        self.appointment.reason = "Fever and cough"
        self.appointment.save()
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.arrived_at, self.earlier)

    def test_status_only_saves_write_the_arrival_time(self):
        # set_status_view and the clinical app save with update_fields=["status", "updated_at"].
        self.set_status(Status.ARRIVED, update_fields=["status", "updated_at"])
        self.assertIsNotNone(self.appointment.arrived_at)
        self.set_status(Status.SCHEDULED, update_fields=["status", "updated_at"])
        self.assertIsNone(self.appointment.arrived_at)

    def test_seen_by_mistake_goes_back_to_the_same_place(self):
        self.arrive_earlier()
        self.set_status(Status.COMPLETED, update_fields=["status", "updated_at"])
        self.assertEqual(self.appointment.arrived_at, self.earlier)
        self.set_status(Status.ARRIVED, update_fields=["status", "updated_at"])
        self.assertEqual(self.appointment.arrived_at, self.earlier)

    def test_arrived_by_mistake_then_really_arriving_gets_a_new_time(self):
        self.arrive_earlier()
        self.set_status(Status.SCHEDULED)
        self.assertIsNone(self.appointment.arrived_at)
        self.set_status(Status.ARRIVED)
        self.assertGreater(self.appointment.arrived_at, self.earlier)

    def test_did_not_come_then_arrived_late_gets_a_new_time(self):
        self.arrive_earlier()
        self.set_status(Status.NO_SHOW)
        self.assertIsNone(self.appointment.arrived_at)
        self.set_status(Status.ARRIVED)
        self.assertGreater(self.appointment.arrived_at, self.earlier)

    def test_cancelling_clears_it(self):
        self.arrive_earlier()
        self.set_status(Status.CANCELLED)
        self.assertIsNone(self.appointment.arrived_at)


class WaitingRoomPageTests(AppointmentTestCase):
    def setUp(self):
        super().setUp()
        self.first_patient = self.make_patient(full_name="Zubair First")
        self.second_patient = self.make_patient(full_name="Amna Second")
        # Booked in the opposite order to how they arrive.
        self.first = self.make_appointment(self.first_patient, when=self.at(self.today, 9, 30), reason="Fever")
        self.second = self.make_appointment(self.second_patient, when=self.at(self.today, 9))
        self.first_arrived = timezone.now() - timedelta(minutes=40)
        self.second_arrived = timezone.now() - timedelta(minutes=25)

    def mark(self, appointment, status):
        response = self.client.post(reverse("appointments:set_status", args=[appointment.pk]), {"status": status})
        self.assertEqual(response.status_code, 302)

    def both_arrive(self):
        """Mark both arrived on the schedule, then pin the arrival times for the assertions."""
        self.mark(self.first, Status.ARRIVED)
        self.mark(self.second, Status.ARRIVED)
        Appointment.objects.filter(pk=self.first.pk).update(arrived_at=self.first_arrived)
        Appointment.objects.filter(pk=self.second.pk).update(arrived_at=self.second_arrived)

    def edit(self, appointment, **changes):
        appointment.refresh_from_db()
        local = timezone.localtime(appointment.scheduled_at, KARACHI)
        data = {
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M"),
            "doctor": appointment.doctor_id or "",
            "duration_minutes": appointment.duration_minutes,
            "reason": appointment.reason,
            "status": appointment.status,
            **changes,
        }
        response = self.client.post(reverse("appointments:update", args=[appointment.pk]), data)
        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)

    def waiting(self, **params):
        response = self.client.get(self.day_url(**params))
        return response, [(a.pk, a.token) for a in response.context["waiting"]]

    def test_editing_a_waiting_patient_keeps_their_place(self):
        self.login(self.receptionist)
        self.both_arrive()
        self.edit(self.first, reason="Fever and cough", doctor=self.owner.pk)

        response, waiting = self.waiting()
        self.assertEqual(waiting, [(self.first.pk, 1), (self.second.pk, 2)])
        here_since = time_format(timezone.localtime(self.first_arrived, KARACHI), "g:i a")
        self.assertContains(response, f"here since {here_since}")
        self.assertContains(response, 'title="Token 1"')

    def test_saving_without_changes_keeps_the_order(self):
        self.login(self.receptionist)
        self.both_arrive()
        self.edit(self.first)
        self.edit(self.second)
        _, waiting = self.waiting()
        self.assertEqual(waiting, [(self.first.pk, 1), (self.second.pk, 2)])

    def test_tokens_do_not_change_when_earlier_patients_are_seen(self):
        third = self.make_appointment(self.make_patient(full_name="Bilal Third"), when=self.at(self.today, 8))
        self.login(self.doctor)
        self.both_arrive()
        self.mark(third, Status.ARRIVED)

        self.mark(self.first, Status.COMPLETED)
        _, waiting = self.waiting()
        self.assertEqual(waiting, [(self.second.pk, 2), (third.pk, 3)])

        # Seen by mistake: back in the queue in the old place, with the old token.
        self.mark(self.first, Status.ARRIVED)
        _, waiting = self.waiting()
        self.assertEqual(waiting, [(self.first.pk, 1), (self.second.pk, 2), (third.pk, 3)])

    def test_tokens_are_numbered_across_the_clinic_even_with_a_doctor_filter(self):
        Appointment.objects.filter(pk=self.second.pk).update(doctor=self.owner)
        self.login(self.receptionist)
        self.both_arrive()
        _, waiting = self.waiting(doctor=self.owner.pk)
        self.assertEqual(waiting, [(self.second.pk, 2)])

    def test_walk_in_gets_an_arrival_time(self):
        self.login(self.receptionist)
        before = timezone.now()
        url = reverse("appointments:create") + f"?patient={self.patient.pk}"
        response = self.client.post(url, {"walk_in": "on", "doctor": self.doctor.pk, "duration_minutes": 15})
        self.assertEqual(response.status_code, 302)
        walk_in = Appointment.objects.get(patient=self.patient)
        self.assertEqual(walk_in.status, Status.ARRIVED)
        self.assertTrue(before <= walk_in.arrived_at <= timezone.now())


class WaitingRoomHelperTests(AppointmentTestCase):
    def test_rows_without_an_arrival_time_fall_back_to_booking_order(self):
        # Only possible with a bulk .update(), which skips save().
        first = self.make_appointment(self.patient, when=self.at(self.today, 9))
        second = self.make_appointment(self.patient, when=self.at(self.today, 8))
        Appointment.objects.filter(pk__in=[first.pk, second.pk]).update(status=Status.ARRIVED)
        waiting = scheduling.waiting_room(Appointment.objects.filter(pk__in=[first.pk, second.pk]))
        self.assertEqual([(a.pk, a.token) for a in waiting], [(first.pk, 1), (second.pk, 2)])

    def test_seen_without_arriving_gets_no_token(self):
        seen = self.make_appointment(self.patient, when=self.at(self.today, 8), status=Status.COMPLETED)
        waiting_appt = self.make_appointment(self.patient, when=self.at(self.today, 9), status=Status.ARRIVED)
        waiting = scheduling.waiting_room([seen, waiting_appt])
        self.assertEqual([(a.pk, a.token) for a in waiting], [(waiting_appt.pk, 1)])
        self.assertFalse(hasattr(seen, "token"))
