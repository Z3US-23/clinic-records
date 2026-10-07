"""refresh_for_appointment(), refresh_for_visit() and refresh_for_patient(): keeping reminders
in step with changes, including bringing back reminders the system skipped."""

from apps.appointments.models import Appointment
from apps.reminders.models import Reminder, ReminderKind
from apps.reminders.services import (
    format_date,
    generate_reminders,
    refresh_for_appointment,
    refresh_for_patient,
    refresh_for_visit,
)

from .base import FROZEN_NOW, FROZEN_TODAY, FrozenClockTestCase

Status = Appointment.Status
PENDING, SENT, SKIPPED = Reminder.Status.PENDING, Reminder.Status.SENT, Reminder.Status.SKIPPED
BY_STAFF, BY_SYSTEM = Reminder.SkipReason.STAFF, Reminder.SkipReason.SYSTEM


class RefreshForAppointmentTests(FrozenClockTestCase):
    def setUp(self):
        super().setUp()
        self.appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 10, 0))

    def reminder(self):
        return Reminder.objects.get(appointment=self.appointment, kind=ReminderKind.APPOINTMENT)

    def set_status(self, status):
        self.appointment.status = status
        self.appointment.save()
        refresh_for_appointment(self.appointment)

    def test_new_appointment_inside_window_gets_its_reminder_at_once(self):
        refresh_for_appointment(self.appointment)
        reminder = self.reminder()
        self.assertEqual(reminder.status, PENDING)
        self.assertEqual(reminder.due_date, FROZEN_TODAY)
        self.assertIn(self.appointment.get_confirm_url(), reminder.message)

    def test_new_appointment_outside_window_waits_for_generation(self):
        later = self.make_appointment(self.patient, when=self.at(self.day(7)))
        refresh_for_appointment(later)
        self.assertFalse(Reminder.objects.filter(appointment=later).exists())

    def test_cancelled_no_show_or_seen_skips_the_pending_reminder(self):
        for status in (Status.CANCELLED, Status.NO_SHOW, Status.COMPLETED):
            with self.subTest(status=status):
                Reminder.objects.all().delete()
                self.appointment.status = Status.SCHEDULED
                refresh_for_appointment(self.appointment)
                self.assertEqual(self.reminder().status, PENDING)

                self.set_status(status)
                self.assertEqual(self.reminder().status, SKIPPED)

    def test_cancelling_never_changes_a_sent_reminder(self):
        refresh_for_appointment(self.appointment)
        Reminder.objects.update(status=SENT, sent_at=FROZEN_NOW, sent_by=self.receptionist)

        self.set_status(Status.CANCELLED)
        self.assertEqual(self.reminder().status, SENT)

    def test_confirmed_appointment_keeps_its_reminder(self):
        refresh_for_appointment(self.appointment)
        self.set_status(Status.CONFIRMED)
        self.assertEqual(self.reminder().status, PENDING)

    def test_reschedule_after_sent_prepares_it_again(self):
        refresh_for_appointment(self.appointment)
        Reminder.objects.update(status=SENT, sent_at=FROZEN_NOW, sent_by=self.receptionist)

        self.appointment.scheduled_at = self.at(self.day(1), 17, 0)
        self.appointment.save()
        refresh_for_appointment(self.appointment, rescheduled=True)

        reminder = self.reminder()
        self.assertEqual(reminder.status, PENDING)
        self.assertIsNone(reminder.sent_at)
        self.assertIsNone(reminder.sent_by)
        self.assertEqual(reminder.due_date, FROZEN_TODAY)
        self.assertIn("5:00 pm", reminder.message)
        self.assertNotIn("10:00 am", reminder.message)

    def test_reschedule_outside_window_removes_it_until_generation_recreates_it(self):
        refresh_for_appointment(self.appointment)
        Reminder.objects.update(status=SENT, sent_at=FROZEN_NOW)

        self.appointment.scheduled_at = self.at(self.day(10), 9, 0)
        self.appointment.save()
        refresh_for_appointment(self.appointment, rescheduled=True)
        self.assertFalse(Reminder.objects.filter(appointment=self.appointment).exists())

        self.assertEqual(generate_reminders(self.clinic, today=self.day(9)), 1)
        reminder = self.reminder()
        self.assertEqual(reminder.due_date, self.day(9))
        self.assertIn(format_date(self.day(10)), reminder.message)

    def test_reschedule_of_a_skipped_reminder_makes_it_pending_again(self):
        self.set_status(Status.RESCHEDULE_REQUESTED)  # nothing to skip yet: no reminder
        refresh_for_appointment(self.appointment)
        self.assertFalse(Reminder.objects.exists())

        Reminder.objects.create(
            clinic=self.clinic, patient=self.patient, appointment=self.appointment,
            kind=ReminderKind.APPOINTMENT, due_date=FROZEN_TODAY, message="old", status=SKIPPED,
        )
        self.appointment.status = Status.SCHEDULED
        self.appointment.scheduled_at = self.at(self.day(1), 12, 0)
        self.appointment.save()
        refresh_for_appointment(self.appointment, rescheduled=True)

        self.assertEqual(self.reminder().status, PENDING)
        self.assertIn("12:00 pm", self.reminder().message)

    def test_edit_without_reschedule_leaves_an_existing_reminder(self):
        refresh_for_appointment(self.appointment)
        Reminder.objects.update(message="Edited by staff")
        refresh_for_appointment(self.appointment)
        self.assertEqual(self.reminder().message, "Edited by staff")

    def test_patient_who_opted_out_gets_none(self):
        self.patient.reminders_opt_in = False
        self.patient.save()
        refresh_for_appointment(self.appointment)
        self.assertFalse(Reminder.objects.exists())


class RefreshForVisitTests(FrozenClockTestCase):
    def setUp(self):
        super().setUp()
        self.old_visit = self.make_visit(self.patient, visit_date=self.at(self.day(-30)), follow_up_date=self.day(-5))

    def test_new_visit_skips_earlier_pending_follow_up_reminders(self):
        overdue = self.make_reminder(kind=ReminderKind.OVERDUE, visit=self.old_visit)
        sent = self.make_reminder(kind=ReminderKind.FOLLOW_UP, visit=self.old_visit, status=SENT)
        other_patient = self.make_patient(full_name="Other Person")
        other_visit = self.make_visit(other_patient, visit_date=self.at(self.day(-30)), follow_up_date=self.day(-5))
        others = self.make_reminder(patient=other_patient, kind=ReminderKind.OVERDUE, visit=other_visit)

        new_visit = self.make_visit(self.patient, visit_date=FROZEN_NOW)
        refresh_for_visit(new_visit)

        overdue.refresh_from_db()
        sent.refresh_from_db()
        others.refresh_from_db()
        self.assertEqual(overdue.status, SKIPPED)
        self.assertEqual(sent.status, SENT)
        self.assertEqual(others.status, PENDING)

    def test_back_dated_visit_does_not_skip_later_visits_reminders(self):
        later_visit = self.make_visit(self.patient, visit_date=self.at(self.day(-10)), follow_up_date=self.day(1))
        follow_up = self.make_reminder(kind=ReminderKind.FOLLOW_UP, visit=later_visit)

        refresh_for_visit(self.old_visit)  # e.g. the doctor corrected an old visit's notes

        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, PENDING)

    def test_unchanged_follow_up_date_keeps_the_reminder(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-1)), follow_up_date=self.day(2))
        generate_reminders(self.clinic)
        reminder = Reminder.objects.get(visit=visit)
        Reminder.objects.filter(pk=reminder.pk).update(message=f"Edited, still {format_date(self.day(2))}.")

        refresh_for_visit(visit)
        self.assertTrue(Reminder.objects.filter(pk=reminder.pk, status=PENDING).exists())

    def test_edited_wording_without_a_date_is_kept(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-1)), follow_up_date=self.day(2))
        reminder = self.make_reminder(kind=ReminderKind.FOLLOW_UP, visit=visit, message="Please come back soon.")

        refresh_for_visit(visit)
        self.assertTrue(Reminder.objects.filter(pk=reminder.pk).exists())

    def test_changed_follow_up_date_replaces_the_reminder(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-1)), follow_up_date=self.day(2))
        generate_reminders(self.clinic)

        visit.follow_up_date = self.day(1)  # still inside the window, but a different date
        visit.save()
        refresh_for_visit(visit)
        self.assertFalse(Reminder.objects.filter(visit=visit).exists())

        generate_reminders(self.clinic)
        self.assertIn(format_date(self.day(1)), Reminder.objects.get(visit=visit).message)

    def test_follow_up_moved_far_ahead_removes_the_reminder(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-1)), follow_up_date=self.day(2))
        generate_reminders(self.clinic)

        visit.follow_up_date = self.day(30)
        visit.save()
        refresh_for_visit(visit)
        self.assertFalse(Reminder.objects.filter(visit=visit).exists())
        self.assertEqual(generate_reminders(self.clinic), 0)  # comes back closer to the date

    def test_cleared_follow_up_date_removes_the_reminder(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-1)), follow_up_date=self.day(2))
        generate_reminders(self.clinic)

        visit.follow_up_date = None
        visit.save()
        refresh_for_visit(visit)
        self.assertFalse(Reminder.objects.filter(visit=visit).exists())

    def test_missed_follow_up_moved_into_the_future_removes_the_overdue_reminder(self):
        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.get(visit=self.old_visit).kind, ReminderKind.OVERDUE)

        self.old_visit.follow_up_date = self.day(5)
        self.old_visit.save()
        refresh_for_visit(self.old_visit)
        self.assertFalse(Reminder.objects.filter(visit=self.old_visit).exists())

    def test_sent_reminders_of_this_visit_are_kept(self):
        sent = self.make_reminder(kind=ReminderKind.OVERDUE, visit=self.old_visit, status=SENT)
        self.old_visit.follow_up_date = None
        self.old_visit.save()
        refresh_for_visit(self.old_visit)
        self.assertTrue(Reminder.objects.filter(pk=sent.pk, status=SENT).exists())


class SkippedBySystemComesBackTests(FrozenClockTestCase):
    """A reminder the system skipped comes back when the reason goes away. One skipped by staff never does."""

    def state(self, reminder):
        reminder.refresh_from_db()
        return reminder.status, reminder.skip_reason

    def set_status(self, appointment, status):
        appointment.status = status
        appointment.save()
        refresh_for_appointment(appointment)

    def test_undoing_a_status_change_brings_the_appointment_reminder_back(self):
        round_trips = [
            (Status.ARRIVED, Status.SCHEDULED),  # "Waiting" tapped by mistake
            (Status.NO_SHOW, Status.SCHEDULED),  # "Did not come", then they rang to say they are coming
            (Status.RESCHEDULE_REQUESTED, Status.CONFIRMED),  # asked for another time, then confirmed after all
        ]
        for away, back in round_trips:
            with self.subTest(away=away, back=back):
                appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 10, 0))
                refresh_for_appointment(appointment)
                reminder = Reminder.objects.get(appointment=appointment)
                Reminder.objects.filter(pk=reminder.pk).update(message="old words")

                self.set_status(appointment, away)
                self.assertEqual(self.state(reminder), (SKIPPED, BY_SYSTEM))

                self.set_status(appointment, back)
                self.assertEqual(self.state(reminder), (PENDING, ""))
                self.assertEqual(reminder.due_date, FROZEN_TODAY)
                self.assertIn(appointment.get_confirm_url(), reminder.message)

    def test_a_reminder_skipped_by_staff_stays_skipped(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 10, 0))
        refresh_for_appointment(appointment)
        reminder = Reminder.objects.get(appointment=appointment)
        Reminder.objects.filter(pk=reminder.pk).update(status=SKIPPED, skip_reason=BY_STAFF)

        self.set_status(appointment, Status.ARRIVED)
        self.set_status(appointment, Status.SCHEDULED)
        for field, away, back in (("reminders_opt_in", False, True), ("is_archived", True, False)):
            setattr(self.patient, field, away)
            self.patient.save()
            refresh_for_patient(self.patient)
            setattr(self.patient, field, back)
            self.patient.save()
            refresh_for_patient(self.patient)
        generate_reminders(self.clinic)

        self.assertEqual(self.state(reminder), (SKIPPED, BY_STAFF))

    def test_outside_the_window_it_comes_back_on_the_right_day(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(5), 10, 0))
        generate_reminders(self.clinic, today=self.day(4))
        reminder = Reminder.objects.get(appointment=appointment)
        self.set_status(appointment, Status.RESCHEDULE_REQUESTED)

        self.set_status(appointment, Status.SCHEDULED)  # today is 5 days before: too early
        self.assertEqual(self.state(reminder), (SKIPPED, BY_SYSTEM))
        generate_reminders(self.clinic, today=self.day(3))
        self.assertEqual(self.state(reminder), (SKIPPED, BY_SYSTEM))

        generate_reminders(self.clinic, today=self.day(4))
        self.assertEqual(self.state(reminder), (PENDING, ""))
        self.assertEqual(reminder.due_date, self.day(4))

    def test_opting_back_in_or_restoring_brings_reminders_back(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 10, 0))
        generate_reminders(self.clinic)
        reminder = Reminder.objects.get(appointment=appointment)

        for field, away, back in (("reminders_opt_in", False, True), ("is_archived", True, False)):
            with self.subTest(field=field):
                setattr(self.patient, field, away)
                self.patient.save()
                generate_reminders(self.clinic)
                self.assertEqual(self.state(reminder), (SKIPPED, BY_SYSTEM))

                setattr(self.patient, field, back)
                self.patient.save()
                self.assertEqual(generate_reminders(self.clinic), 1)
                self.assertEqual(self.state(reminder), (PENDING, ""))

    def test_missed_follow_up_comes_back_when_the_booking_falls_through(self):
        self.make_visit(self.patient, visit_date=self.at(self.day(-30)), follow_up_date=self.day(-5))
        outcomes = [
            (Status.CANCELLED, self.at(self.day(2))),
            (Status.NO_SHOW, self.at(self.day(0), 9, 0)),
        ]
        for outcome, when in outcomes:
            with self.subTest(outcome=outcome):
                generate_reminders(self.clinic)
                overdue = Reminder.objects.get(kind=ReminderKind.OVERDUE)
                self.assertEqual(self.state(overdue), (PENDING, ""))

                booking = self.make_appointment(self.patient, when=when)
                generate_reminders(self.clinic)
                self.assertEqual(self.state(overdue), (SKIPPED, BY_SYSTEM))

                Appointment.objects.filter(pk=booking.pk).update(status=outcome)
                generate_reminders(self.clinic)
                self.assertEqual(self.state(overdue), (PENDING, ""))
                self.assertEqual(overdue.due_date, FROZEN_TODAY)

    def test_missed_follow_up_comes_back_when_a_booking_goes_stale(self):
        """Booked for this morning: not chased today. Still never marked tomorrow: chased again."""
        self.make_visit(self.patient, visit_date=self.at(self.day(-30)), follow_up_date=self.day(-5))
        generate_reminders(self.clinic)
        overdue = Reminder.objects.get(kind=ReminderKind.OVERDUE)
        self.make_appointment(self.patient, when=self.at(self.day(0), 9, 0))

        generate_reminders(self.clinic)
        self.assertEqual(self.state(overdue), (SKIPPED, BY_SYSTEM))

        generate_reminders(self.clinic, today=self.day(1))
        self.assertEqual(self.state(overdue), (PENDING, ""))
        self.assertEqual(overdue.due_date, self.day(1))

    def test_follow_up_comes_back_when_the_patient_wants_another_time(self):
        self.make_visit(self.patient, visit_date=self.at(self.day(-20)), follow_up_date=self.day(1))
        generate_reminders(self.clinic)
        follow_up = Reminder.objects.get(kind=ReminderKind.FOLLOW_UP)
        booking = self.make_appointment(self.patient, when=self.at(self.day(3)))
        generate_reminders(self.clinic)
        self.assertEqual(self.state(follow_up), (SKIPPED, BY_SYSTEM))

        self.set_status(booking, Status.RESCHEDULE_REQUESTED)
        generate_reminders(self.clinic)

        self.assertEqual(self.state(follow_up), (PENDING, ""))

    def test_too_old_to_chase_does_not_come_back(self):
        visit = self.make_visit(self.patient, visit_date=self.at(self.day(-90)), follow_up_date=self.day(-61))
        overdue = self.make_reminder(kind=ReminderKind.OVERDUE, visit=visit, status=SKIPPED, skip_reason=BY_SYSTEM)

        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(self.state(overdue), (SKIPPED, BY_SYSTEM))


class RefreshForPatientTests(FrozenClockTestCase):
    def setUp(self):
        super().setUp()
        self.appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 10, 0))
        generate_reminders(self.clinic)
        self.automatic = Reminder.objects.get(appointment=self.appointment)
        self.custom = self.make_reminder(message="Your report is ready.")
        self.others = self.make_reminder(patient=self.make_patient(full_name="Someone Else"))

    def change(self, **fields):
        for name, value in fields.items():
            setattr(self.patient, name, value)
        self.patient.save()
        refresh_for_patient(self.patient)

    def states(self):
        rows = {"automatic": self.automatic, "custom": self.custom, "others": self.others}
        return {
            name: Reminder.objects.values_list("status", "skip_reason").get(pk=reminder.pk)
            for name, reminder in rows.items()
        }

    def test_opting_out_skips_automatic_reminders_at_once(self):
        self.change(reminders_opt_in=False)
        self.assertEqual(
            self.states(),
            {"automatic": (SKIPPED, BY_SYSTEM), "custom": (PENDING, ""), "others": (PENDING, "")},
        )

    def test_archiving_skips_custom_messages_too(self):
        self.change(is_archived=True)
        self.assertEqual(
            self.states(),
            {"automatic": (SKIPPED, BY_SYSTEM), "custom": (SKIPPED, BY_SYSTEM), "others": (PENDING, "")},
        )

    def test_opting_back_in_brings_reminders_back_at_once(self):
        self.change(reminders_opt_in=False)
        self.change(reminders_opt_in=True)
        self.assertEqual(self.states()["automatic"], (PENDING, ""))

    def test_sent_reminders_are_never_changed(self):
        Reminder.objects.filter(pk=self.automatic.pk).update(status=SENT, sent_at=FROZEN_NOW)
        self.change(is_archived=True)
        self.assertEqual(self.states()["automatic"], (SENT, ""))
