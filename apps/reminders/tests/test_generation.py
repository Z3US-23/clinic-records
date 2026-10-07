"""generate_reminders(): which reminders are prepared, when, and the tidying of stale ones."""

from datetime import datetime, timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import Clinic, Membership
from apps.appointments.models import Appointment
from apps.core.testing import make_appointment, make_clinic, make_patient, make_user, make_visit
from apps.reminders import services
from apps.reminders.models import Reminder, ReminderKind
from apps.reminders.services import generate_reminders

from .base import FROZEN_TODAY, KARACHI, KOLKATA, UTC, FrozenClockTestCase

Status = Appointment.Status
PENDING, SENT, SKIPPED = Reminder.Status.PENDING, Reminder.Status.SENT, Reminder.Status.SKIPPED


class AppointmentReminderTests(FrozenClockTestCase):
    """Clock: Tue 6 Oct 2026, 11:00 am in Karachi. Default: remind 1 day before."""

    def test_appointment_tomorrow_gets_a_reminder_due_today(self):
        self.clinic.phone = "042-3576-1234"
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 15, 30))

        self.assertEqual(generate_reminders(self.clinic), 1)

        reminder = Reminder.objects.get(appointment=appointment)
        self.assertEqual(reminder.kind, ReminderKind.APPOINTMENT)
        self.assertEqual(reminder.status, PENDING)
        self.assertEqual(reminder.due_date, FROZEN_TODAY)
        self.assertEqual(reminder.clinic, self.clinic)
        self.assertEqual(reminder.patient, self.patient)
        for text in ("Hello Ali", "Al-Noor Family Clinic", "Dr. Bilal Hussain", "Wed 7 Oct", "3:30 pm",
                     "Tap to confirm or ask for another time: " + appointment.get_confirm_url(), "042-3576-1234"):
            self.assertIn(text, reminder.message)

    def test_appointment_without_a_doctor_uses_friendly_wording(self):
        make_appointment(self.patient, None, self.at(self.day(1)))
        generate_reminders(self.clinic)
        self.assertIn("with the doctor on", Reminder.objects.get().message)

    def test_any_doctor_in_a_one_doctor_clinic_names_that_doctor(self):
        solo = make_clinic("Solo Clinic")
        make_user(solo, Membership.Role.OWNER, full_name="Sana Iqbal")
        make_user(solo, Membership.Role.RECEPTIONIST)
        patient = make_patient(solo)
        make_appointment(patient, None, self.at(self.day(1)))  # booked as "any doctor" before the fix
        generate_reminders(solo)
        self.assertIn("with Dr. Sana Iqbal on", Reminder.objects.get(clinic=solo).message)

    def test_window_edges(self):
        self.clinic.appointment_reminder_days = 2
        later_today = self.make_appointment(self.patient, when=self.at(self.day(0), 18, 0))
        last_minute_of_window = self.make_appointment(self.patient, when=self.at(self.day(2), 23, 59))
        first_minute_after = self.make_appointment(self.patient, when=self.at(self.day(3), 0, 0))
        earlier_today = self.make_appointment(self.patient, when=self.at(self.day(0), 9, 0))  # already over

        self.assertEqual(generate_reminders(self.clinic), 2)

        with_reminder = set(Reminder.objects.values_list("appointment", flat=True))
        self.assertEqual(with_reminder, {later_today.pk, last_minute_of_window.pk})
        self.assertNotIn(first_minute_after.pk, with_reminder)
        self.assertNotIn(earlier_today.pk, with_reminder)
        self.assertEqual(set(Reminder.objects.values_list("due_date", flat=True)), {FROZEN_TODAY})

    def test_due_date_is_n_days_before_the_appointment(self):
        self.clinic.appointment_reminder_days = 3
        appointment = self.make_appointment(self.patient, when=self.at(self.day(5)))

        self.assertEqual(generate_reminders(self.clinic, today=self.day(1)), 0)  # 4 days before: too early
        self.assertEqual(generate_reminders(self.clinic, today=self.day(2)), 1)  # 3 days before
        self.assertEqual(Reminder.objects.get(appointment=appointment).due_date, self.day(2))

    def test_only_scheduled_and_confirmed_appointments(self):
        for status in Status.values:
            self.make_appointment(self.patient, when=self.at(self.day(1)), status=status)

        generate_reminders(self.clinic)

        statuses = set(Reminder.objects.values_list("appointment__status", flat=True))
        self.assertEqual(statuses, {Status.SCHEDULED, Status.CONFIRMED})

    def test_opted_out_and_archived_patients_get_none(self):
        opted_out = self.make_patient(full_name="Sana Opt", reminders_opt_in=False)
        archived = self.make_patient(full_name="Zara Archive", is_archived=True)
        for patient in (opted_out, archived):
            self.make_appointment(patient, when=self.at(self.day(1)))

        self.assertEqual(generate_reminders(self.clinic), 0)

    def test_missing_whatsapp_number_does_not_block_creation(self):
        no_number = self.make_patient(full_name="Kamran Shah", phone="12345")
        self.assertEqual(no_number.whatsapp_number, "")
        self.make_appointment(no_number, when=self.at(self.day(1)))

        self.assertEqual(generate_reminders(self.clinic), 1)

    def test_other_clinics_appointments_are_left_alone(self):
        other_patient = make_patient(self.other_clinic, full_name="Other Patient")
        make_appointment(other_patient, self.other_owner, self.at(self.day(1)))

        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(generate_reminders(self.other_clinic), 1)
        self.assertEqual(Reminder.objects.get().clinic, self.other_clinic)

    def test_uses_the_clinics_own_template(self):
        self.clinic.message_templates.create(
            kind=ReminderKind.APPOINTMENT, body="Salam {first_name}! {date} {time} {confirm_link} {first_name.__class__}"
        )
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1), 9, 15))
        generate_reminders(self.clinic)
        self.assertEqual(
            Reminder.objects.get().message,
            f"Salam Ali! Wed 7 Oct 9:15 am {appointment.get_confirm_url()} {{first_name.__class__}}",
        )


class FollowUpReminderTests(FrozenClockTestCase):
    """Default: remind 2 days before the follow-up date."""

    def visit(self, follow_up_date, days_ago=10, patient=None, **kwargs):
        return self.make_visit(
            patient or self.patient, visit_date=self.at(self.day(-days_ago)), follow_up_date=follow_up_date, **kwargs
        )

    def test_follow_up_in_window_gets_a_reminder_due_today(self):
        visit = self.visit(self.day(2))
        self.assertEqual(generate_reminders(self.clinic), 1)

        reminder = Reminder.objects.get(visit=visit)
        self.assertEqual(reminder.kind, ReminderKind.FOLLOW_UP)
        self.assertEqual(reminder.due_date, FROZEN_TODAY)
        self.assertIn("Thu 8 Oct", reminder.message)
        self.assertIn("follow-up check-up", reminder.message)

    def test_window_edges(self):
        due_today = self.visit(self.day(0), patient=self.make_patient(full_name="A One"))
        last_day = self.visit(self.day(2), patient=self.make_patient(full_name="B Two"))
        too_early = self.visit(self.day(3), patient=self.make_patient(full_name="C Three"))
        yesterday = self.visit(self.day(-1), patient=self.make_patient(full_name="D Four"))

        generate_reminders(self.clinic)

        with_reminder = set(Reminder.objects.filter(kind=ReminderKind.FOLLOW_UP).values_list("visit", flat=True))
        self.assertEqual(with_reminder, {due_today.pk, last_day.pk})
        self.assertNotIn(too_early.pk, with_reminder)
        self.assertNotIn(yesterday.pk, with_reminder)

    def test_due_date_is_n_days_before_the_follow_up(self):
        visit = self.visit(self.day(6))
        self.assertEqual(generate_reminders(self.clinic, today=self.day(3)), 0)
        self.assertEqual(generate_reminders(self.clinic, today=self.day(4)), 1)
        self.assertEqual(Reminder.objects.get(visit=visit).due_date, self.day(4))

    def test_not_needed_after_a_later_visit(self):
        self.visit(self.day(1), days_ago=10)
        self.visit(None, days_ago=2)  # came back already

        self.assertEqual(generate_reminders(self.clinic), 0)

    def test_not_needed_when_an_appointment_is_booked(self):
        self.visit(self.day(1))
        self.make_appointment(self.patient, when=self.at(self.day(1), 12, 0))

        generate_reminders(self.clinic)
        self.assertFalse(Reminder.objects.filter(kind=ReminderKind.FOLLOW_UP).exists())

    def test_cancelled_or_missed_appointments_do_not_count_as_booked(self):
        for status in (Status.CANCELLED, Status.NO_SHOW):
            with self.subTest(status=status):
                Reminder.objects.all().delete()
                Appointment.objects.all().delete()
                self.visit(self.day(1))
                self.make_appointment(self.patient, when=self.at(self.day(-1)), status=status)

                generate_reminders(self.clinic)
                self.assertEqual(Reminder.objects.filter(kind=ReminderKind.FOLLOW_UP).count(), 1)

    def test_wants_another_time_does_not_count_as_booked(self):
        # The patient turned that time down, so a "please book a time that suits you" message is right.
        self.visit(self.day(1))
        self.make_appointment(self.patient, when=self.at(self.day(1), 12, 0), status=Status.RESCHEDULE_REQUESTED)

        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.filter(kind=ReminderKind.FOLLOW_UP, status=PENDING).count(), 1)

    def test_the_visits_own_appointment_does_not_count_as_booked(self):
        # The doctor wrote up the visit a little before the booked time.
        appointment = self.make_appointment(self.patient, when=self.at(self.day(-10), 11, 0), status=Status.COMPLETED)
        self.make_visit(self.patient, visit_date=self.at(self.day(-10), 10, 50),
                        follow_up_date=self.day(1), appointment=appointment)

        self.assertEqual(generate_reminders(self.clinic), 1)

    def test_opted_out_and_archived_patients_get_none(self):
        self.visit(self.day(1), patient=self.make_patient(full_name="Sana Opt", reminders_opt_in=False))
        self.visit(self.day(1), patient=self.make_patient(full_name="Zara Archive", is_archived=True))

        self.assertEqual(generate_reminders(self.clinic), 0)


class OverdueReminderTests(FrozenClockTestCase):
    """Default: a follow-up is 'missed' 3 days after its date; not chased after 60 days."""

    def visit(self, follow_up_date, patient=None, days_ago=70):
        return self.make_visit(
            patient or self.patient, visit_date=self.at(self.day(-days_ago)), follow_up_date=follow_up_date
        )

    def test_missed_follow_up_gets_a_reminder_due_today(self):
        visit = self.visit(self.day(-3))
        self.assertEqual(generate_reminders(self.clinic), 1)

        reminder = Reminder.objects.get(visit=visit)
        self.assertEqual(reminder.kind, ReminderKind.OVERDUE)
        self.assertEqual(reminder.due_date, FROZEN_TODAY)
        self.assertIn("was due on Sat 3 Oct", reminder.message)

    def test_window_edges(self):
        grace_not_over = self.visit(self.day(-2), self.make_patient(full_name="A One"))
        grace_over = self.visit(self.day(-3), self.make_patient(full_name="B Two"))
        oldest_chased = self.visit(self.day(-60), self.make_patient(full_name="C Three"))
        too_old = self.visit(self.day(-61), self.make_patient(full_name="D Four"))

        generate_reminders(self.clinic)

        with_reminder = set(Reminder.objects.filter(kind=ReminderKind.OVERDUE).values_list("visit", flat=True))
        self.assertEqual(with_reminder, {grace_over.pk, oldest_chased.pk})
        self.assertNotIn(grace_not_over.pk, with_reminder)
        self.assertNotIn(too_old.pk, with_reminder)

    def test_not_needed_after_a_later_visit_or_booking(self):
        self.visit(self.day(-5))
        other = self.make_patient(full_name="Nadia Booked")
        self.visit(self.day(-5), other)
        self.make_visit(self.patient, visit_date=self.at(self.day(-4)))  # came back
        self.make_appointment(other, when=self.at(self.day(3)))  # booked

        generate_reminders(self.clinic)
        self.assertFalse(Reminder.objects.filter(kind=ReminderKind.OVERDUE).exists())

    def missed_with_appointment(self, status, when):
        """A new patient whose follow-up was due 5 days ago, with one appointment after the visit."""
        patient = self.make_patient(full_name=f"Patient {status.label}")
        visit = self.visit(self.day(-5), patient)
        self.make_appointment(patient, when=when, status=status)
        return visit

    def test_stale_bookings_do_not_count_as_coming_back(self):
        """Booked or Confirmed for a day that has passed and never closed, or "Wants another time"."""
        visits = [
            self.missed_with_appointment(status, self.at(self.day(-4)))
            for status in (Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED)
        ]

        generate_reminders(self.clinic)

        prepared = Reminder.objects.filter(kind=ReminderKind.OVERDUE, status=PENDING)
        self.assertEqual(set(prepared.values_list("visit", flat=True)), {visit.pk for visit in visits})

    def test_an_attended_appointment_counts_as_coming_back(self):
        for status in (Status.ARRIVED, Status.COMPLETED):
            self.missed_with_appointment(status, self.at(self.day(-4)))

        self.assertEqual(generate_reminders(self.clinic), 0)

    def test_todays_booking_counts_for_the_whole_day(self):
        # Booked for 8 am; it is 11 am and nobody has marked them yet: they may be running late.
        self.missed_with_appointment(Status.SCHEDULED, self.at(self.day(0), 8, 0))

        self.assertEqual(generate_reminders(self.clinic), 0)

    def test_a_grace_of_zero_still_waits_until_the_day_after(self):
        """Nobody is told they missed a check-up on the day it is due, whatever the setting says."""
        Clinic.objects.filter(pk=self.clinic.pk).update(overdue_grace_days=0)
        self.clinic.refresh_from_db()
        self.visit(self.day(0), days_ago=20)

        generate_reminders(self.clinic)
        self.assertEqual(list(Reminder.objects.values_list("kind", "status")), [(ReminderKind.FOLLOW_UP, PENDING)])

        generate_reminders(self.clinic, today=self.day(1))  # the day after: now it is missed
        self.assertEqual(Reminder.objects.get(kind=ReminderKind.OVERDUE).status, PENDING)
        self.assertEqual(Reminder.objects.get(kind=ReminderKind.FOLLOW_UP).status, SKIPPED)

    def test_replaces_a_follow_up_reminder_that_was_never_sent(self):
        visit = self.visit(self.day(-3))
        follow_up = self.make_reminder(kind=ReminderKind.FOLLOW_UP, visit=visit, due_date=self.day(-5))

        generate_reminders(self.clinic)

        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, SKIPPED)
        self.assertEqual(Reminder.objects.get(kind=ReminderKind.OVERDUE).status, PENDING)

    def test_sent_follow_up_reminder_is_kept_and_missed_one_still_prepared(self):
        visit = self.visit(self.day(-3))
        follow_up = self.make_reminder(kind=ReminderKind.FOLLOW_UP, visit=visit, status=SENT)

        self.assertEqual(generate_reminders(self.clinic), 1)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, SENT)


class IdempotencyTests(FrozenClockTestCase):
    def setUp(self):
        super().setUp()
        self.make_appointment(self.patient, when=self.at(self.day(1)))
        self.make_visit(self.make_patient(full_name="Fatima Bibi"), visit_date=self.at(self.day(-20)),
                        follow_up_date=self.day(1))
        self.make_visit(self.make_patient(full_name="Usman Ghani"), visit_date=self.at(self.day(-20)),
                        follow_up_date=self.day(-4))

    def test_second_run_creates_nothing(self):
        self.assertEqual(generate_reminders(self.clinic), 3)
        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(Reminder.objects.count(), 3)

    def test_skipped_or_sent_reminders_are_not_prepared_again(self):
        generate_reminders(self.clinic)
        Reminder.objects.filter(kind=ReminderKind.APPOINTMENT).update(status=SKIPPED)
        Reminder.objects.filter(kind=ReminderKind.FOLLOW_UP).update(status=SENT)

        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(Reminder.objects.count(), 3)

    def test_reminders_skipped_by_staff_are_not_prepared_again(self):
        generate_reminders(self.clinic)
        Reminder.objects.update(status=SKIPPED, skip_reason=Reminder.SkipReason.STAFF)

        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertFalse(Reminder.objects.filter(status=PENDING).exists())

    def test_reminders_skipped_by_the_system_come_back_once_and_are_not_duplicated(self):
        generate_reminders(self.clinic)
        Reminder.objects.update(status=SKIPPED, skip_reason=Reminder.SkipReason.SYSTEM)

        self.assertEqual(generate_reminders(self.clinic), 3)  # still needed, so brought back
        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(Reminder.objects.count(), 3)
        self.assertEqual(set(Reminder.objects.values_list("status", "skip_reason")), {(PENDING, "")})

    def test_a_duplicate_insert_is_ignored_not_raised(self):
        """Two requests generating at the same moment: the unique constraint wins quietly."""
        generate_reminders(self.clinic)
        existing = Reminder.objects.get(kind=ReminderKind.APPOINTMENT)
        created = services._create_reminder(
            clinic=self.clinic, patient=existing.patient, appointment=existing.appointment,
            kind=ReminderKind.APPOINTMENT, due_date=existing.due_date, message="duplicate",
        )
        self.assertFalse(created)
        self.assertEqual(Reminder.objects.filter(kind=ReminderKind.APPOINTMENT).count(), 1)

    def test_query_count_does_not_grow_with_the_number_of_patients(self):
        generate_reminders(self.clinic)
        with CaptureQueriesContext(connection) as small:
            generate_reminders(self.clinic)

        for n in range(6):
            patient = self.make_patient(full_name=f"Extra Patient{n}")
            self.make_appointment(patient, when=self.at(self.day(1), 11, n))
            self.make_visit(patient, visit_date=self.at(self.day(-20)), follow_up_date=self.day(-4))
        generate_reminders(self.clinic)
        with CaptureQueriesContext(connection) as large:
            generate_reminders(self.clinic)

        self.assertEqual(len(large), len(small))
        self.assertLess(len(large), 20)


class StaleReminderTests(FrozenClockTestCase):
    """Reminders that are no longer needed are marked skipped; sent ones are never touched."""

    def test_cancelled_appointment(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1)))
        generate_reminders(self.clinic)
        Appointment.objects.filter(pk=appointment.pk).update(status=Status.CANCELLED)

        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.get().status, SKIPPED)

    def test_appointment_time_has_passed(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(0), 18, 0))
        generate_reminders(self.clinic)
        Appointment.objects.filter(pk=appointment.pk).update(scheduled_at=self.at(self.day(0), 10, 0))

        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.get().status, SKIPPED)

    def test_sent_reminders_are_never_changed(self):
        appointment = self.make_appointment(self.patient, when=self.at(self.day(1)))
        generate_reminders(self.clinic)
        Reminder.objects.update(status=SENT)
        Appointment.objects.filter(pk=appointment.pk).update(status=Status.CANCELLED)

        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.get().status, SENT)

    def test_follow_up_after_patient_came_back_or_booked(self):
        came_back = self.make_patient(full_name="Came Back")
        booked = self.make_patient(full_name="Booked Later")
        for patient in (came_back, booked):
            self.make_visit(patient, visit_date=self.at(self.day(-20)), follow_up_date=self.day(1))
        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.filter(status=PENDING).count(), 2)

        self.make_visit(came_back, visit_date=self.at(self.day(-1)))
        self.make_appointment(booked, when=self.at(self.day(2)))
        generate_reminders(self.clinic)

        self.assertFalse(Reminder.objects.filter(status=PENDING).exists())
        self.assertEqual(Reminder.objects.filter(status=SKIPPED).count(), 2)
        self.assertEqual(set(Reminder.objects.values_list("skip_reason", flat=True)), {Reminder.SkipReason.SYSTEM})

    def test_a_stale_past_booking_does_not_skip_follow_up_reminders(self):
        """A booking for a day that has passed, never marked Seen or Did not come, is not "coming back"."""
        due_soon = self.make_patient(full_name="Due Soon")
        missed = self.make_patient(full_name="Missed It")
        self.make_visit(due_soon, visit_date=self.at(self.day(-20)), follow_up_date=self.day(1))
        self.make_visit(missed, visit_date=self.at(self.day(-20)), follow_up_date=self.day(-5))
        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.filter(status=PENDING).count(), 2)

        for patient in (due_soon, missed):
            self.make_appointment(patient, when=self.at(self.day(-2)))  # still "Booked"
        generate_reminders(self.clinic)

        self.assertEqual(Reminder.objects.filter(status=PENDING).count(), 2)

    def test_patient_opted_out_or_archived(self):
        opted_out = self.make_patient(full_name="Opted Out")
        archived = self.make_patient(full_name="Archived Later")
        self.make_visit(opted_out, visit_date=self.at(self.day(-20)), follow_up_date=self.day(-5))
        self.make_appointment(archived, when=self.at(self.day(1)))
        generate_reminders(self.clinic)
        self.assertEqual(Reminder.objects.filter(status=PENDING).count(), 2)

        opted_out.reminders_opt_in = False
        opted_out.save()
        archived.is_archived = True
        archived.save()
        generate_reminders(self.clinic)

        self.assertEqual(Reminder.objects.filter(status=SKIPPED).count(), 2)

    def test_custom_messages_are_left_alone(self):
        custom = self.make_reminder(due_date=self.day(-10))
        generate_reminders(self.clinic)
        custom.refresh_from_db()
        self.assertEqual(custom.status, PENDING)


class ClinicTimezoneTests(FrozenClockTestCase):
    """Near midnight the two countries are on different dates.

    Clock: Tue 6 Oct 2026 18:45 UTC = 11:45 pm (6 Oct) in Karachi, 12:15 am (7 Oct) in Kolkata.
    """

    now = datetime(2026, 10, 6, 18, 45, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        self.india = make_clinic("Delhi Care Clinic", country=Clinic.Country.INDIA, timezone="Asia/Kolkata")
        self.india_doctor = make_user(self.india, Membership.Role.DOCTOR, full_name="Priya Sharma")
        self.india_patient = make_patient(self.india, full_name="Rahul Verma", phone="98765 43210")

    def test_each_clinic_uses_its_own_today(self):
        make_appointment(self.patient, self.doctor, datetime(2026, 10, 8, 10, 0, tzinfo=KARACHI))
        make_appointment(self.india_patient, self.india_doctor, datetime(2026, 10, 8, 10, 0, tzinfo=KOLKATA))

        # Karachi: still 6 Oct, so 8 Oct is 2 days away (outside the 1-day window).
        self.assertEqual(generate_reminders(self.clinic), 0)
        # Kolkata: already 7 Oct, so 8 Oct is tomorrow.
        self.assertEqual(generate_reminders(self.india), 1)
        self.assertEqual(Reminder.objects.get().due_date, datetime(2026, 10, 7).date())

    def test_local_day_boundaries(self):
        # Karachi window: 6-7 Oct (local).
        karachi_in = make_appointment(self.patient, self.doctor, datetime(2026, 10, 7, 18, 50, tzinfo=UTC))  # 11:50 pm 7 Oct
        karachi_out = make_appointment(self.patient, self.doctor, datetime(2026, 10, 7, 19, 10, tzinfo=UTC))  # 12:10 am 8 Oct
        # Kolkata window: 7-8 Oct (local).
        india_in = make_appointment(self.india_patient, self.india_doctor, datetime(2026, 10, 8, 18, 20, tzinfo=UTC))  # 11:50 pm 8 Oct
        india_out = make_appointment(self.india_patient, self.india_doctor, datetime(2026, 10, 8, 18, 40, tzinfo=UTC))  # 12:10 am 9 Oct

        generate_reminders(self.clinic)
        generate_reminders(self.india)

        with_reminder = set(Reminder.objects.values_list("appointment", flat=True))
        self.assertEqual(with_reminder, {karachi_in.pk, india_in.pk})
        self.assertNotIn(karachi_out.pk, with_reminder)
        self.assertNotIn(india_out.pk, with_reminder)

    def test_message_shows_the_clinics_local_date_and_time(self):
        self.clinic.appointment_reminder_days = 2
        self.india.appointment_reminder_days = 2
        moment = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)  # 12:00 am Thu 8 Oct Karachi, 12:30 am Kolkata
        make_appointment(self.patient, self.doctor, moment)
        make_appointment(self.india_patient, self.india_doctor, moment)

        generate_reminders(self.clinic)
        generate_reminders(self.india)

        karachi = Reminder.objects.get(clinic=self.clinic).message
        india = Reminder.objects.get(clinic=self.india).message
        self.assertIn("on Thu 8 Oct at 12:00 am", karachi)
        self.assertIn("on Thu 8 Oct at 12:30 am", india)
        self.assertIn("Dr. Priya Sharma", india)

    def test_follow_up_window_uses_the_clinics_today(self):
        self.make_visit(self.patient, visit_date=datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
                        follow_up_date=datetime(2026, 10, 9).date())
        make_visit(self.india_patient, self.india_doctor, visit_date=datetime(2026, 9, 1, 6, 0, tzinfo=UTC),
                   follow_up_date=datetime(2026, 10, 9).date())

        # 2-day window: Karachi 6-8 Oct (no), Kolkata 7-9 Oct (yes).
        self.assertEqual(generate_reminders(self.clinic), 0)
        self.assertEqual(generate_reminders(self.india), 1)

    def test_today_can_be_given_explicitly(self):
        make_appointment(self.patient, self.doctor, datetime(2026, 10, 8, 10, 0, tzinfo=KARACHI))
        self.assertEqual(generate_reminders(self.clinic, today=self.today + timedelta(days=1)), 1)
