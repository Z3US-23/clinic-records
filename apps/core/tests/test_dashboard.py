from datetime import UTC, datetime, time, timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.appointments.models import Appointment
from apps.core.testing import make_clinic, make_user
from apps.core.views import greeting_for
from apps.reminders.models import Reminder

from .base import KARACHI, CoreTestCase

DASHBOARD = reverse("core:dashboard")

# The dashboard prepares reminders on every visit. Most tests switch that off so the
# counts only reflect the reminders each test creates.
NO_GENERATION = mock.patch("apps.core.views.generate_reminders", return_value=0)


class DashboardAccessTests(CoreTestCase):
    def test_sign_in_required(self):
        response = self.client.get(DASHBOARD)
        self.assertRedirects(response, f"{reverse('accounts:login')}?next=/", fetch_redirect_response=False)

    def test_user_without_a_clinic_is_sent_to_no_clinic_page(self):
        loner = User.objects.create_user(email="loner@example.test", password="x-pass-12345", full_name="No Clinic")
        self.login(loner)
        response = self.client.get(DASHBOARD)
        self.assertRedirects(response, reverse("accounts:no_clinic"), fetch_redirect_response=False)

    @NO_GENERATION
    def test_renders_for_every_role(self, _generate):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(role=user.memberships.get().role):
                self.login(user)
                response = self.client.get(DASHBOARD)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "core/dashboard.html")

    def test_post_is_not_allowed(self):
        self.login(self.owner)
        self.assertEqual(self.client.post(DASHBOARD).status_code, 405)


class GreetingTests(CoreTestCase):
    def test_greeting_by_time_of_day(self):
        self.assertEqual(greeting_for(datetime(2026, 10, 6, 8, 0, tzinfo=KARACHI)), "Good morning")
        self.assertEqual(greeting_for(datetime(2026, 10, 6, 13, 0, tzinfo=KARACHI)), "Good afternoon")
        self.assertEqual(greeting_for(datetime(2026, 10, 6, 19, 30, tzinfo=KARACHI)), "Good evening")

    @NO_GENERATION
    def test_greeting_uses_the_clinic_timezone(self, _generate):
        # 08:30 at the clinic is 03:30 UTC (the server's clock): it must still say "morning".
        clinic_morning = datetime.combine(self.today, time(8, 30), tzinfo=KARACHI)
        self.login(self.owner)
        with mock.patch("django.utils.timezone.now", return_value=clinic_morning.astimezone(UTC)):
            response = self.client.get(DASHBOARD)
        self.assertContains(response, "Good morning, Dr. Sara")
        self.assertContains(response, f"{self.today.day} {self.today:%b %Y}")
        self.assertContains(response, self.clinic.name)

    @NO_GENERATION
    def test_receptionist_greeted_by_first_name(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, ", Hina</h1>")


class DashboardStatsTests(CoreTestCase):
    @NO_GENERATION
    def test_stat_counts(self, _generate):
        patient = self.make_patient(full_name="Ayesha Khan")
        other = self.make_patient(full_name="Kamran Akmal")
        archived = self.make_patient(full_name="Old Archived", is_archived=True)
        last_month = self.make_patient(full_name="Registered Last Month")
        last_month.created_at = self.at(self.today.replace(day=1) - timedelta(days=3), 10)
        last_month.save(update_fields=["created_at"])

        # Appointments: 2 today (one waiting); the cancelled one, tomorrow's and another clinic's don't count.
        self.make_appointment(patient, when=self.at(self.today, 10), status=Appointment.Status.ARRIVED)
        self.make_appointment(other, when=self.at(self.today, 11))
        self.make_appointment(other, when=self.at(self.today, 12), status=Appointment.Status.CANCELLED)
        self.make_appointment(patient, when=self.at(self.days_from_today(1), 10))
        stranger = self.make_patient(self.other_clinic, full_name="Other Clinic Patient")
        self.make_appointment(stranger, doctor=self.other_owner, when=self.at(self.today, 10))

        # Reminders: due today or earlier = "to send"; pending OVERDUE = missed follow-ups.
        self.make_reminder(patient, due_date=self.today)
        self.make_reminder(other, due_date=self.days_from_today(-2))
        self.make_reminder(patient, kind=Reminder.Kind.OVERDUE, due_date=self.today)
        self.make_reminder(other, kind=Reminder.Kind.OVERDUE, due_date=self.days_from_today(1))
        self.make_reminder(patient, due_date=self.days_from_today(1))  # not due yet
        self.make_reminder(patient, status=Reminder.Status.SENT)  # already sent
        self.make_reminder(stranger, due_date=self.today)  # other clinic

        self.login(self.doctor)
        response = self.client.get(DASHBOARD)
        stats = response.context["stats"]
        self.assertEqual(stats["appointments_today"], 2)
        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(stats["reminders_due"], 3)
        self.assertEqual(stats["missed_follow_ups"], 2)
        self.assertEqual(stats["patients"], 3)  # archived patients are not counted
        self.assertEqual(stats["new_this_month"], 2)
        self.assertNotIn("Other Clinic Patient", response.content.decode())
        self.assertTrue(archived.is_archived)

    @NO_GENERATION
    def test_missed_follow_ups_tile_turns_red(self, _generate):
        patient = self.make_patient()
        self.login(self.doctor)
        self.assertNotContains(self.client.get(DASHBOARD), "stat stat-danger")
        self.make_reminder(patient, kind=Reminder.Kind.OVERDUE)
        self.assertContains(self.client.get(DASHBOARD), "stat stat-danger")

    @NO_GENERATION
    def test_tiles_link_to_their_pages(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        for name in ("appointments:day", "reminders:list", "patients:list"):
            self.assertContains(response, f'href="{reverse(name)}"')


class DashboardGeneratesRemindersTests(CoreTestCase):
    def test_reminders_are_prepared_for_this_clinic(self):
        self.login(self.receptionist)
        with mock.patch("apps.core.views.generate_reminders", return_value=0) as generate:
            self.client.get(DASHBOARD)
        generate.assert_called_once_with(self.clinic)

    def test_a_reminder_problem_does_not_break_the_page(self):
        self.login(self.receptionist)
        with mock.patch("apps.core.views.generate_reminders", side_effect=RuntimeError("boom")), \
                self.assertLogs("apps.core.views", "ERROR"):
            response = self.client.get(DASHBOARD)
        self.assertEqual(response.status_code, 200)


class WaitingRoomTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.make_patient(full_name="Zubair First")
        self.second = self.make_patient(full_name="Amna Second")
        # Booked in the opposite order to how they arrived.
        self.appt_second = self.make_appointment(self.second, when=self.at(self.today, 10), status=Appointment.Status.ARRIVED)
        self.appt_first = self.make_appointment(self.first, when=self.at(self.today, 11), status=Appointment.Status.ARRIVED)
        Appointment.objects.filter(pk=self.appt_first.pk).update(updated_at=self.at(self.today, 9, 40))
        Appointment.objects.filter(pk=self.appt_second.pk).update(updated_at=self.at(self.today, 9, 55))

    @NO_GENERATION
    def test_waiting_room_in_arrival_order_with_tokens(self, _generate):
        self.login(self.receptionist)
        waiting = self.client.get(DASHBOARD).context["waiting"]
        self.assertEqual([a.patient.full_name for a in waiting], ["Zubair First", "Amna Second"])
        self.assertEqual([a.token for a in waiting], [1, 2])

    @NO_GENERATION
    def test_clinicians_can_start_a_visit_from_the_waiting_room(self, _generate):
        start_url = f"{reverse('clinical:visit_create', args=[self.first.pk])}?appointment={self.appt_first.pk}"
        for user in (self.owner, self.doctor):
            self.login(user)
            self.assertContains(self.client.get(DASHBOARD), start_url)
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertNotContains(response, "Start visit")
        self.assertNotContains(response, reverse("clinical:visit_create", args=[self.first.pk]))


class ClinicalPrivacyTests(CoreTestCase):
    """Receptionists never see diagnoses or other clinical text on the dashboard."""

    def setUp(self):
        super().setUp()
        self.patient = self.make_patient(
            full_name="Rabia Malik", allergies="SECRET-ALLERGY", chronic_conditions="SECRET-CONDITION"
        )
        self.make_visit(
            self.patient,
            chief_complaint="SECRET-COMPLAINT",
            diagnosis="SECRET-DIAGNOSIS",
            plan="SECRET-PLAN",
            follow_up_date=self.days_from_today(3),
        )
        self.make_appointment(self.patient, when=self.at(self.today, 10), status=Appointment.Status.ARRIVED)

    @NO_GENERATION
    def test_receptionist_sees_no_clinical_text(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        content = response.content.decode()
        self.assertIn("Rabia Malik", content)  # the patient is listed...
        for secret in ("SECRET-ALLERGY", "SECRET-CONDITION", "SECRET-COMPLAINT", "SECRET-DIAGNOSIS", "SECRET-PLAN"):
            self.assertNotIn(secret, content)  # ...but nothing clinical
        self.assertNotContains(response, "Your recent visits")
        self.assertEqual(response.context["recent_visits"], [])

    @NO_GENERATION
    def test_doctor_sees_own_recent_visits_with_diagnosis(self, _generate):
        self.login(self.doctor)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, "Your recent visits")
        self.assertContains(response, "SECRET-DIAGNOSIS")
        self.assertNotContains(response, "SECRET-ALLERGY")

    @NO_GENERATION
    def test_recent_visits_are_the_last_five_of_this_doctor(self, _generate):
        now = self.at(self.today, 9)
        for days_ago in range(6):
            self.make_visit(self.patient, visit_date=now - timedelta(days=days_ago + 1), diagnosis=f"Doctor visit {days_ago}")
        self.make_visit(self.patient, doctor=self.owner, visit_date=now, diagnosis="Owner's visit")
        self.login(self.doctor)
        visits = self.client.get(DASHBOARD).context["recent_visits"]
        self.assertEqual(len(visits), 5)
        self.assertTrue(all(v.doctor_id == self.doctor.pk for v in visits))
        dates = [v.visit_date for v in visits]
        self.assertEqual(dates, sorted(dates, reverse=True))


class ComingBackSoonTests(CoreTestCase):
    @NO_GENERATION
    def test_follow_ups_in_the_next_week(self, _generate):
        soon = self.make_patient(full_name="Due In Three Days")
        self.make_visit(soon, follow_up_date=self.days_from_today(3), visit_date=self.at(self.days_from_today(-20), 10))

        later = self.make_patient(full_name="Due In Ten Days")
        self.make_visit(later, follow_up_date=self.days_from_today(10))

        seen_again = self.make_patient(full_name="Already Came Back")
        self.make_visit(seen_again, follow_up_date=self.days_from_today(2), visit_date=self.at(self.days_from_today(-10), 10))
        self.make_visit(seen_again, visit_date=self.at(self.days_from_today(-1), 10))

        archived = self.make_patient(full_name="Archived Patient", is_archived=True)
        self.make_visit(archived, follow_up_date=self.days_from_today(1))

        stranger = self.make_patient(self.other_clinic, full_name="Other Clinic Patient")
        self.make_visit(stranger, doctor=self.other_owner, follow_up_date=self.days_from_today(1))

        self.login(self.receptionist)
        names = [v.patient.full_name for v in self.client.get(DASHBOARD).context["coming_back"]]
        self.assertEqual(names, ["Due In Three Days"])


class RemindersCardTests(CoreTestCase):
    @NO_GENERATION
    def test_one_tap_send_buttons(self, _generate):
        with_whatsapp = self.make_patient(full_name="Has Whatsapp", phone="0300-1234567")
        no_number = self.make_patient(full_name="No Number", phone="12")
        sendable = self.make_reminder(with_whatsapp)
        blocked = self.make_reminder(no_number)

        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, f'action="{reverse("reminders:send", args=[sendable.pk])}"')
        self.assertNotContains(response, reverse("reminders:send", args=[blocked.pk]))
        self.assertContains(response, "No WhatsApp number")
        self.assertContains(response, 'method="post"')

    @NO_GENERATION
    def test_shows_at_most_five(self, _generate):
        patient = self.make_patient()
        for _ in range(7):
            self.make_reminder(patient)
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertEqual(len(response.context["reminders_to_send"]), 5)
        self.assertContains(response, "See all reminders (7)")


class OnboardingTests(CoreTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.new_clinic = make_clinic("Brand New Clinic")
        cls.new_owner = make_user(cls.new_clinic, Membership.Role.OWNER, email="new-owner@example.test", full_name="Nadia Iqbal")
        cls.new_doctor = make_user(cls.new_clinic, Membership.Role.DOCTOR, email="new-doc@example.test")

    @NO_GENERATION
    def test_owner_of_a_new_clinic_gets_a_checklist(self, _generate):
        self.login(self.new_owner)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, "Get your clinic ready")
        for name in ("patients:create", "patients:import", "accounts:staff_list", "accounts:clinic_settings", "reminders:templates"):
            self.assertContains(response, f'href="{reverse(name)}"')
        steps = {step["label"]: step["done"] for step in response.context["onboarding_steps"]}
        self.assertTrue(steps["Add staff"])  # the clinic already has a second member
        self.assertFalse(steps["Add your first patient"])

    @NO_GENERATION
    def test_no_checklist_for_doctors_or_once_patients_exist(self, _generate):
        self.login(self.new_doctor)
        self.assertNotContains(self.client.get(DASHBOARD), "Get your clinic ready")
        self.login(self.new_owner)
        self.make_patient(self.new_clinic)
        self.assertNotContains(self.client.get(DASHBOARD), "Get your clinic ready")


class DashboardQueryTests(CoreTestCase):
    """The page does the same number of queries however many rows it shows (no N+1)."""

    def add_rows(self, count):
        for n in range(count):
            patient = self.make_patient(full_name=f"Patient {self._rows + n}")
            self.make_appointment(patient, when=self.at(self.today, 10, n), status=Appointment.Status.ARRIVED)
            self.make_appointment(patient, when=self.at(self.today, 11, n), doctor=self.owner)
            self.make_reminder(patient)
            self.make_visit(patient, follow_up_date=self.days_from_today(2), visit_date=self.at(self.days_from_today(-3), 9, n))
        self._rows += count

    def count_queries(self):
        self.client.get(DASHBOARD)  # warm-up: the first request also remembers the clinic in the session
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(DASHBOARD).status_code, 200)
        return len(queries)

    @NO_GENERATION
    def test_query_count_does_not_grow_with_rows(self, _generate):
        self._rows = 0
        self.login(self.doctor)
        self.add_rows(1)
        few = self.count_queries()
        self.add_rows(4)
        many = self.count_queries()
        self.assertEqual(few, many)
