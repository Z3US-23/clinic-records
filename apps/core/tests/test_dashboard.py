from datetime import UTC, datetime, time, timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django.utils import timezone

from apps.accounts.models import Clinic, Membership, User
from apps.appointments.models import Appointment
from apps.core.models import AuditLog, SetupChecklist
from apps.core.testing import make_clinic, make_user
from apps.core.views import greeting_for
from apps.reminders.models import MessageTemplate, Reminder
from apps.reminders.services import DEFAULT_TEMPLATES

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

        # Reminders: due today or earlier = "to send".
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
        # Missed follow-ups come from visits (see MissedFollowUpTests), not from pending messages.
        self.assertEqual(stats["missed_follow_ups"], 0)
        self.assertEqual(stats["patients"], 3)  # archived patients are not counted
        self.assertEqual(stats["new_this_month"], 2)
        self.assertNotIn("Other Clinic Patient", response.content.decode())
        self.assertTrue(archived.is_archived)

    @NO_GENERATION
    def test_missed_follow_ups_tile_turns_red(self, _generate):
        patient = self.make_patient()
        self.login(self.doctor)
        self.assertNotContains(self.client.get(DASHBOARD), "stat stat-danger")
        self.make_visit(patient, visit_date=self.at(self.days_from_today(-30), 10), follow_up_date=self.days_from_today(-10))
        self.assertContains(self.client.get(DASHBOARD), "stat stat-danger")

    @NO_GENERATION
    def test_tiles_link_to_their_pages(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        for name in ("appointments:day", "reminders:list", "core:missed_follow_ups", "patients:list"):
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
        Appointment.objects.filter(pk=self.appt_first.pk).update(arrived_at=self.at(self.today, 9, 40))
        Appointment.objects.filter(pk=self.appt_second.pk).update(arrived_at=self.at(self.today, 9, 55))

    def waiting(self):
        return [(a.pk, a.token) for a in self.client.get(DASHBOARD).context["waiting"]]

    @NO_GENERATION
    def test_waiting_room_in_arrival_order_with_tokens(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        waiting = response.context["waiting"]
        self.assertEqual([a.patient.full_name for a in waiting], ["Zubair First", "Amna Second"])
        self.assertEqual([a.token for a in waiting], [1, 2])
        self.assertContains(response, "here since 9:40 a.m.")

    @NO_GENERATION
    def test_editing_a_waiting_patient_keeps_their_place(self, _generate):
        self.login(self.receptionist)
        before = self.waiting()
        local = timezone.localtime(self.appt_first.scheduled_at, KARACHI)
        response = self.client.post(
            reverse("appointments:update", args=[self.appt_first.pk]),
            {
                "date": local.date().isoformat(),
                "time": local.strftime("%H:%M"),
                "doctor": self.owner.pk,
                "duration_minutes": self.appt_first.duration_minutes,
                "reason": "Fever and cough",
                "status": Appointment.Status.ARRIVED,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.appt_first.refresh_from_db()
        self.assertEqual((self.appt_first.doctor, self.appt_first.reason), (self.owner, "Fever and cough"))
        self.assertEqual(self.waiting(), before)

    @NO_GENERATION
    def test_tokens_match_the_day_view(self, _generate):
        self.login(self.receptionist)
        day_view = self.client.get(reverse("appointments:day"))
        self.assertEqual(self.waiting(), [(a.pk, a.token) for a in day_view.context["waiting"]])

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

    def steps(self):
        response = self.client.get(DASHBOARD)
        return response, {step["label"]: step["done"] for step in response.context["onboarding_steps"]}

    @NO_GENERATION
    def test_owner_of_a_new_clinic_gets_a_checklist(self, _generate):
        self.login(self.new_owner)
        response, steps = self.steps()
        self.assertContains(response, "Get your clinic ready")
        for name in ("patients:create", "patients:import", "accounts:staff_list", "accounts:clinic_settings", "reminders:templates"):
            self.assertContains(response, f'href="{reverse(name)}"')
        self.assertEqual(steps, {
            "Add your patients": False,
            "Add staff": False,
            "Set clinic details for prescriptions": False,
            "Review reminder messages": False,
        })
        self.assertContains(response, f'action="{reverse("core:hide_setup_checklist")}"')

    @NO_GENERATION
    def test_checklist_stays_after_the_first_patient(self, _generate):
        make_user(self.new_clinic, Membership.Role.DOCTOR, email="new-doc@example.test")
        self.make_patient(self.new_clinic)
        self.login(self.new_owner)
        response, steps = self.steps()
        self.assertContains(response, "Get your clinic ready")
        self.assertTrue(steps["Add your patients"])
        self.assertTrue(steps["Add staff"])  # the clinic has a second member now
        self.assertFalse(steps["Review reminder messages"])
        self.assertEqual(response.context["onboarding_done"], 2)

    @NO_GENERATION
    def test_a_waiting_invitation_is_not_staff_yet(self, _generate):
        Membership(user=self.doctor, clinic=self.new_clinic, role=Membership.Role.DOCTOR).start_invitation()
        self.login(self.new_owner)
        _, steps = self.steps()
        self.assertFalse(steps["Add staff"])

    @NO_GENERATION
    def test_keeping_the_standard_wording_ticks_the_reminder_step(self, _generate):
        self.login(self.new_owner)
        kind = Reminder.Kind.APPOINTMENT
        response = self.client.post(
            reverse("reminders:templates"), {"kind": kind, "body": DEFAULT_TEMPLATES[kind], "action": "save"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MessageTemplate.objects.filter(clinic=self.new_clinic).exists())  # nothing custom stored
        _, steps = self.steps()
        self.assertTrue(steps["Review reminder messages"])

    @NO_GENERATION
    def test_gone_once_every_step_is_done(self, _generate):
        make_user(self.new_clinic, Membership.Role.RECEPTIONIST, email="new-desk@example.test")
        self.make_patient(self.new_clinic)
        Clinic.objects.filter(pk=self.new_clinic.pk).update(phone="042-35761234", address="1 Mall Road")
        MessageTemplate.objects.create(clinic=self.new_clinic, kind=Reminder.Kind.FOLLOW_UP, body="Hello {first_name}")
        self.login(self.new_owner)
        response = self.client.get(DASHBOARD)
        self.assertNotContains(response, "Get your clinic ready")
        self.assertEqual(response.context["onboarding_steps"], [])

    @NO_GENERATION
    def test_owner_can_hide_it(self, _generate):
        self.login(self.new_owner)
        response = self.client.post(reverse("core:hide_setup_checklist"))
        self.assertRedirects(response, DASHBOARD, fetch_redirect_response=False)
        self.assertNotContains(self.client.get(DASHBOARD), "Get your clinic ready")
        self.assertEqual(SetupChecklist.objects.get(clinic=self.new_clinic).hidden_by, self.new_owner)
        self.assertTrue(AuditLog.objects.filter(
            clinic=self.new_clinic, action=AuditLog.Action.UPDATE, summary="Hid the setup checklist"
        ).exists())
        # Only that clinic's checklist is hidden.
        self.assertFalse(SetupChecklist.objects.filter(clinic=self.clinic).exists())

    def test_hiding_is_post_only_and_owner_only(self):
        url = reverse("core:hide_setup_checklist")
        self.login(self.new_owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        for user in (self.doctor, self.receptionist):
            self.login(user)
            self.assertEqual(self.client.post(url).status_code, 403)
        self.assertFalse(SetupChecklist.objects.exists())

    @NO_GENERATION
    def test_never_shown_to_doctors_or_receptionists(self, _generate):
        for user in (self.doctor, self.receptionist):
            self.login(user)
            response = self.client.get(DASHBOARD)
            self.assertNotContains(response, "Get your clinic ready")
            self.assertEqual(response.context["onboarding_steps"], [])


class RescheduleRequestTests(CoreTestCase):
    """Patients who tap "I need another time" on their link show up on Today until someone acts."""

    def setUp(self):
        super().setUp()
        self.patient = self.make_patient(full_name="Rashid Mehmood")
        self.appointment = self.make_appointment(self.patient, when=self.at(self.days_from_today(1), 17, 30))

    def ask_for_another_time(self, note="Friday evening is better for me"):
        url = reverse("public:confirm", args=[self.appointment.confirm_token])
        response = self.client.post(url, {"answer": "reschedule", "note": note})
        self.assertEqual(response.status_code, 302)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.RESCHEDULE_REQUESTED)

    def test_request_is_listed_with_note_and_actions(self):
        self.ask_for_another_time(note="Friday <b>evening</b> is better")
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, "Asking for another time")
        self.assertEqual([a.pk for a in response.context["asking_new_time"]], [self.appointment.pk])
        self.assertEqual(response.context["stats"]["reschedule_requests"], 1)
        self.assertContains(response, "Rashid Mehmood")
        self.assertContains(response, "Booked for Tomorrow, 5:30")
        self.assertContains(response, "Friday &lt;b&gt;evening&lt;/b&gt; is better")  # the patient's text is escaped
        self.assertContains(response, f'href="{reverse("appointments:update", args=[self.appointment.pk])}?next=/"')
        self.assertContains(response, 'href="tel:+923001234567"')
        self.assertContains(response, f'href="{reverse("reminders:create")}?patient={self.patient.pk}"')

    def test_badge_on_today_from_every_page(self):
        self.ask_for_another_time()
        self.login(self.doctor)
        response = self.client.get(reverse("patients:list"))
        self.assertEqual(response.context["nav_reschedule_requests"], 1)
        self.assertContains(response, "asking for another time</span>")

    def test_gone_once_the_time_is_changed(self):
        self.ask_for_another_time()
        self.login(self.receptionist)
        response = self.client.post(
            f"{reverse('appointments:update', args=[self.appointment.pk])}?next=/",
            {
                "date": self.days_from_today(3).isoformat(), "time": "18:00", "doctor": self.doctor.pk,
                "duration_minutes": 15, "reason": "", "status": Appointment.Status.RESCHEDULE_REQUESTED,
            },
        )
        self.assertRedirects(response, DASHBOARD, fetch_redirect_response=False)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, Appointment.Status.SCHEDULED)
        response = self.client.get(DASHBOARD)
        self.assertEqual(response.context["asking_new_time"], [])
        self.assertNotContains(response, "Asking for another time")
        self.assertEqual(response.context["nav_reschedule_requests"], 0)

    @NO_GENERATION
    def test_only_open_requests_from_today_on_in_this_clinic(self, _generate):
        wants_another_time = Appointment.Status.RESCHEDULE_REQUESTED
        earlier_today = self.make_appointment(
            self.make_patient(full_name="Earlier Today"), when=self.at(self.today, 0, 5), status=wants_another_time
        )
        self.make_appointment(
            self.make_patient(full_name="Yesterday Patient"), when=self.at(self.days_from_today(-1), 10),
            status=wants_another_time,
        )
        self.make_appointment(
            self.make_patient(full_name="Cancelled Patient"), when=self.at(self.days_from_today(2), 10),
            status=Appointment.Status.CANCELLED,
        )
        stranger = self.make_patient(self.other_clinic, full_name="Other Clinic Patient")
        self.make_appointment(stranger, doctor=self.other_owner, when=self.at(self.days_from_today(1), 10), status=wants_another_time)
        Appointment.objects.filter(pk=self.appointment.pk).update(status=wants_another_time)

        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertEqual([a.pk for a in response.context["asking_new_time"]], [earlier_today.pk, self.appointment.pk])
        self.assertEqual(response.context["nav_reschedule_requests"], 2)
        for name in ("Yesterday Patient", "Cancelled Patient", "Other Clinic Patient"):
            self.assertNotContains(response, name)

    @NO_GENERATION
    def test_lists_ten_but_counts_all(self, _generate):
        for n in range(12):
            patient = self.make_patient(full_name=f"Patient {n}")
            self.make_appointment(
                patient, when=self.at(self.days_from_today(2), 10, n), status=Appointment.Status.RESCHEDULE_REQUESTED
            )
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertEqual(len(response.context["asking_new_time"]), 10)
        self.assertEqual(response.context["stats"]["reschedule_requests"], 12)
        self.assertContains(response, "Showing the 10 soonest of 12.")


class TodayActionsTests(CoreTestCase):
    """The front desk marks patients Arrived (and Seen, Did not come...) straight from Today."""

    def setUp(self):
        super().setUp()
        self.patient = self.make_patient(full_name="Walk Up Patient")
        self.appointment = self.make_appointment(self.patient, when=self.at(self.today, 23, 45))
        self.status_url = reverse("appointments:set_status", args=[self.appointment.pk])

    @NO_GENERATION
    def test_booked_appointment_has_quick_buttons(self, _generate):
        self.login(self.receptionist)
        response = self.client.get(DASHBOARD)
        self.assertContains(response, f'action="{self.status_url}"')
        self.assertContains(response, ">Arrived</button>")
        self.assertContains(response, 'data-confirm="Cancel this appointment?"')

    @NO_GENERATION
    def test_arrived_goes_to_the_waiting_room_and_back_to_today(self, _generate):
        self.login(self.receptionist)
        response = self.client.post(self.status_url, {"status": Appointment.Status.ARRIVED, "next": DASHBOARD})
        self.assertRedirects(response, DASHBOARD, fetch_redirect_response=False)
        waiting = self.client.get(DASHBOARD).context["waiting"]
        self.assertEqual([a.pk for a in waiting], [self.appointment.pk])

    @NO_GENERATION
    def test_seen_appointments_have_no_buttons(self, _generate):
        Appointment.objects.filter(pk=self.appointment.pk).update(status=Appointment.Status.COMPLETED)
        self.login(self.receptionist)
        self.assertNotContains(self.client.get(DASHBOARD), f'action="{self.status_url}"')


class MissedFollowUpTests(CoreTestCase):
    """Patients whose follow-up date passed and who neither came back nor booked."""

    URL = reverse("core:missed_follow_ups")

    def missed_visit(self, patient, days_late=10, **kwargs):
        return self.make_visit(
            patient, visit_date=self.at(self.days_from_today(-days_late - 20), 10),
            follow_up_date=self.days_from_today(-days_late), diagnosis="SECRET-DIAGNOSIS", **kwargs,
        )

    def listed(self, user=None):
        self.login(user or self.receptionist)
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        return response, [visit.patient.full_name for visit in response.context["page_obj"]]

    @NO_GENERATION
    def test_listed_whatever_happened_to_the_message(self, _generate):
        messaged = self.make_patient(full_name="Already Messaged")
        self.make_reminder(
            messaged, kind=Reminder.Kind.OVERDUE, visit=self.missed_visit(messaged),
            status=Reminder.Status.SENT, sent_at=timezone.now(),
        )
        skipped = self.make_patient(full_name="Message Skipped")
        self.make_reminder(
            skipped, kind=Reminder.Kind.OVERDUE, visit=self.missed_visit(skipped, days_late=20),
            status=Reminder.Status.SKIPPED,
        )
        self.missed_visit(self.make_patient(full_name="Opted Out", reminders_opt_in=False), days_late=30)
        self.missed_visit(self.make_patient(full_name="No Mobile", phone="12"), days_late=40)
        self.missed_visit(self.make_patient(full_name="Months Late"), days_late=120)  # WhatsApp gave up after 60 days

        self.login(self.receptionist)
        self.assertEqual(self.client.get(DASHBOARD).context["stats"]["missed_follow_ups"], 5)
        response, names = self.listed()
        self.assertEqual(names, ["Already Messaged", "Message Skipped", "Opted Out", "No Mobile", "Months Late"])
        for text in ("Message sent", "Message skipped", "Doesn&#x27;t want reminders: call", "No WhatsApp number: call",
                     "Not messaged", "10 days late"):
            self.assertContains(response, text)

    @NO_GENERATION
    def test_coming_back_or_booking_takes_a_patient_off(self, _generate):
        came_back = self.make_patient(full_name="Came Back")
        self.missed_visit(came_back)
        self.make_visit(came_back, visit_date=self.at(self.days_from_today(-2), 10))
        booked = self.make_patient(full_name="Booked Again")
        self.missed_visit(booked)
        self.make_appointment(booked, when=self.at(self.days_from_today(2), 10))
        cancelled = self.make_patient(full_name="Cancelled Booking")
        self.missed_visit(cancelled)
        self.make_appointment(cancelled, when=self.at(self.days_from_today(2), 10), status=Appointment.Status.CANCELLED)
        self.missed_visit(self.make_patient(full_name="Archived Patient", is_archived=True))
        self.missed_visit(self.make_patient(full_name="Inside Grace Days"), days_late=2)  # the clinic waits 3 days
        self.missed_visit(self.make_patient(full_name="Too Long Ago"), days_late=200)
        stranger = self.make_patient(self.other_clinic, full_name="Other Clinic Patient")
        self.missed_visit(stranger, doctor=self.other_owner)

        _, names = self.listed()
        self.assertEqual(names, ["Cancelled Booking"])  # a cancelled booking is not a booking

    @NO_GENERATION
    def test_call_and_book_but_no_clinical_text(self, _generate):
        patient = self.make_patient(full_name="Ayesha Khan", allergies="SECRET-ALLERGY")
        self.missed_visit(patient)
        response, _ = self.listed(self.receptionist)
        self.assertContains(response, 'href="tel:+923001234567"')
        self.assertContains(response, f'href="{reverse("appointments:create")}?patient={patient.pk}"')
        self.assertContains(response, patient.mrn)
        self.assertNotContains(response, "SECRET-DIAGNOSIS")
        self.assertNotContains(response, "SECRET-ALLERGY")

    @NO_GENERATION
    def test_other_clinic_sees_only_its_own(self, _generate):
        self.missed_visit(self.make_patient(full_name="Our Patient"))
        _, names = self.listed(self.other_owner)
        self.assertEqual(names, [])

    def test_sign_in_required_and_get_only(self):
        self.assertEqual(self.client.get(self.URL).status_code, 302)
        self.login(self.receptionist)
        self.assertEqual(self.client.post(self.URL).status_code, 405)

    @NO_GENERATION
    def test_empty_state(self, _generate):
        response, _ = self.listed()
        self.assertContains(response, "Nobody has missed a follow-up")


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
