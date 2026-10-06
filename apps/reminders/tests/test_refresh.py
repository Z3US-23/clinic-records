"""refresh_for_appointment() and refresh_for_visit(): keeping reminders in step with changes."""

from apps.appointments.models import Appointment
from apps.reminders.models import Reminder, ReminderKind
from apps.reminders.services import format_date, generate_reminders, refresh_for_appointment, refresh_for_visit

from .base import FROZEN_NOW, FROZEN_TODAY, FrozenClockTestCase

Status = Appointment.Status
PENDING, SENT, SKIPPED = Reminder.Status.PENDING, Reminder.Status.SENT, Reminder.Status.SKIPPED


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
