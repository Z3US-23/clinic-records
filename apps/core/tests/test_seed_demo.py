from datetime import UTC, datetime, time, timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Clinic, Membership, User
from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem, Visit
from apps.core.management.commands.seed_demo import DEFAULT_PASSWORD, DEMO_EMAILS, DEMO_SLUG
from apps.core.models import AuditLog
from apps.core.testing import make_user
from apps.patients.models import Patient
from apps.reminders.models import Reminder

from .base import KARACHI, CoreTestCase

Status = Appointment.Status


def seed(*args):
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


def clinic_time_today(hour, minute=0):
    """Today at hour:minute in Lahore, as the UTC moment the server's clock would show."""
    today = timezone.localdate(timezone.now(), KARACHI)
    return datetime.combine(today, time(hour, minute), tzinfo=KARACHI).astimezone(UTC)


def seed_at(moment, *args):
    """Run seed_demo as if the clock showed `moment`."""
    with mock.patch("django.utils.timezone.now", return_value=moment):
        return seed(*args)


def other_clinics_snapshot():
    """Everything that belongs to clinics other than the demo clinic."""
    others = Clinic.objects.exclude(slug=DEMO_SLUG)
    return {
        "clinics": list(others.order_by("pk").values_list("pk", "name", "slug")),
        "memberships": list(Membership.objects.filter(clinic__in=others).order_by("pk").values_list("pk", "user_id", "role")),
        "users": list(User.objects.exclude(email__in=DEMO_EMAILS).order_by("pk").values_list("pk", "email", "password")),
        "patients": list(Patient.objects.filter(clinic__in=others).order_by("pk").values_list("pk", "full_name", "phone")),
        "visits": list(Visit.objects.filter(clinic__in=others).order_by("pk").values_list("pk", "diagnosis")),
        "appointments": list(Appointment.objects.filter(clinic__in=others).order_by("pk").values_list("pk", "status")),
        "reminders": list(Reminder.objects.filter(clinic__in=others).order_by("pk").values_list("pk", "status")),
        "audit": list(AuditLog.objects.exclude(clinic__slug=DEMO_SLUG).order_by("pk").values_list("pk", "user_id", "summary")),
    }


class SeedDemoTests(CoreTestCase):
    """The default-size demo: one run shared by the checks below (seeding takes a few seconds).

    Seeded at 4 pm clinic time, so the counts of today's appointments don't depend on when the
    tests run (see SeedDemoClockTests for other times of day).
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.output = seed_at(clinic_time_today(16))
        cls.demo = Clinic.objects.get(slug=DEMO_SLUG)

    def test_clinic_and_logins(self):
        self.assertEqual(self.demo.name, "Demo Family Clinic")
        self.assertEqual((self.demo.city, self.demo.timezone, self.demo.country), ("Lahore", "Asia/Karachi", "PK"))
        self.assertTrue(self.demo.phone and self.demo.prescription_header and self.demo.prescription_footer)
        # The printed prescription shows clinic.phone itself: the header must not repeat it.
        self.assertNotIn(self.demo.phone, self.demo.prescription_header)
        roles = {m.user.email: m for m in self.demo.memberships.select_related("user")}
        self.assertEqual(set(roles), set(DEMO_EMAILS))
        owner = roles["demo-owner@clinic.test"]
        self.assertEqual((owner.role, owner.display_name), (Membership.Role.OWNER, "Dr. Sara Ahmed"))
        self.assertEqual(owner.qualifications, "MBBS, FCPS (Medicine)")
        self.assertTrue(owner.registration_number)
        doctor = roles["demo-doctor@clinic.test"]
        self.assertEqual((doctor.role, doctor.display_name), (Membership.Role.DOCTOR, "Dr. Bilal Hussain"))
        self.assertEqual(doctor.qualifications, "MBBS, MCPS (Family Medicine)")
        reception = roles["demo-reception@clinic.test"]
        self.assertEqual((reception.role, reception.display_name), (Membership.Role.RECEPTIONIST, "Hina Malik"))
        for email in DEMO_EMAILS:
            self.assertTrue(User.objects.get(email=email).check_password(DEFAULT_PASSWORD))

    def test_summary_output(self):
        for text in (*DEMO_EMAILS, DEFAULT_PASSWORD, f"{settings.SITE_URL}/", "60 patients"):
            self.assertIn(text, self.output)

    def test_phone_numbers_are_unallocated(self):
        patients = Patient.objects.filter(clinic=self.demo)
        self.assertEqual(patients.count(), 60)
        for patient in patients:
            with self.subTest(patient=patient.full_name):
                if patient.phone:
                    self.assertTrue(patient.phone.startswith("0390-"))
                if patient.whatsapp_phone:
                    self.assertTrue(patient.whatsapp_phone.startswith("0390-"))
                if patient.whatsapp_number:
                    self.assertTrue(patient.whatsapp_number.startswith("92390"))
        self.assertTrue(self.demo.phone.startswith("0390-"))

    def test_realistic_patients(self):
        patients = list(Patient.objects.filter(clinic=self.demo))
        ages = [p.age for p in patients if p.age is not None]
        self.assertTrue(any(age < 13 for age in ages), "some children")
        self.assertTrue(any(age >= 65 for age in ages), "some elderly patients")
        self.assertEqual({p.sex for p in patients}, {"M", "F"})
        self.assertTrue(any(p.date_of_birth is None for p in patients))
        self.assertTrue(any(p.guardian_name for p in patients))
        opted_out = sum(not p.reminders_opt_in for p in patients)
        self.assertTrue(3 <= opted_out <= 9, opted_out)
        allergies = " ".join(p.allergies for p in patients)
        for allergy in ("Penicillin", "Sulfa drugs", "NSAIDs"):
            self.assertIn(allergy, allergies)
        conditions = " ".join(p.chronic_conditions for p in patients)
        for condition in ("Type 2 diabetes", "Hypertension", "Asthma", "Hypothyroidism", "Ischaemic heart disease"):
            self.assertIn(condition, conditions)
        self.assertEqual(len({p.mrn for p in patients}), 60)

    def test_visits_prescriptions_and_labs(self):
        today = self.today
        visits = Visit.objects.filter(clinic=self.demo)
        self.assertGreater(visits.count(), 60)
        self.assertFalse(visits.filter(visit_date__date__lt=today - timedelta(days=366)).exists())
        for patient in Patient.objects.filter(clinic=self.demo).exclude(created_at__date__gte=today.replace(day=1)):
            self.assertTrue(1 <= patient.visits.count() <= 7, patient.full_name)
        medicines = set(PrescriptionItem.objects.filter(visit__clinic=self.demo).values_list("medicine", flat=True))
        for medicine in ("Tab. Panadol 500mg", "Tab. Metformin 500mg"):
            self.assertIn(medicine, medicines)
        self.assertTrue(PrescriptionItem.objects.filter(visit__clinic=self.demo, frequency="1+0+1").exists())
        self.assertTrue(LabResult.objects.filter(clinic=self.demo, test_name="HbA1c", result_text__startswith="8.4%", is_abnormal=True).exists())
        self.assertFalse(LabResult.objects.filter(clinic=self.demo).exclude(file="").exists())
        # Penicillin-allergic patients are never prescribed Augmentin / Amoxil.
        allergic = Patient.objects.filter(clinic=self.demo, allergies__contains="Penicillin")
        self.assertFalse(
            PrescriptionItem.objects.filter(visit__patient__in=allergic, medicine__regex=r"Augmentin|Amoxil").exists()
        )

    def test_todays_appointments(self):
        today = Appointment.objects.filter(clinic=self.demo, scheduled_at__date=self.today)
        self.assertTrue(10 <= today.count() <= 12)
        self.assertEqual(today.filter(status=Status.ARRIVED).count(), 2)
        self.assertEqual(today.filter(status=Status.NO_SHOW).count(), 1)
        completed = today.filter(status=Status.COMPLETED)
        self.assertEqual(completed.count(), 3)
        for appointment in completed:
            self.assertTrue(appointment.visits.exists(), "a 'Seen' appointment has its visit")
        reschedule = today.get(status=Status.RESCHEDULE_REQUESTED)
        self.assertTrue(reschedule.patient_note)
        self.assertEqual(
            set(today.values_list("status", flat=True)),
            {Status.COMPLETED, Status.NO_SHOW, Status.ARRIVED, Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED},
        )
        self.assertEqual({a.doctor_id for a in today if a.doctor_id}, {m.user_id for m in self.demo.memberships.filter(role__in=Membership.CLINICAL_ROLES)})

        tomorrow = Appointment.objects.filter(clinic=self.demo, scheduled_at__date=self.today + timedelta(days=1)).exclude(status=Status.CANCELLED)
        self.assertTrue(6 <= tomorrow.count() <= 8)
        # One patient asked for another time, so the Today page's call-back card is never empty in a demo.
        self.assertEqual(tomorrow.filter(status=Status.RESCHEDULE_REQUESTED).count(), 1)
        later = Appointment.objects.filter(
            clinic=self.demo, scheduled_at__date__gt=self.today + timedelta(days=1),
            scheduled_at__date__lte=self.today + timedelta(days=14),
        )
        self.assertGreater(later.count(), 5)

    def test_follow_ups_and_reminders(self):
        today = self.today
        pending = Reminder.objects.filter(clinic=self.demo, status=Reminder.Status.PENDING, due_date__lte=today)
        self.assertGreaterEqual(pending.filter(kind=Reminder.Kind.OVERDUE).count(), 5)
        self.assertTrue(pending.filter(kind=Reminder.Kind.APPOINTMENT).exists())
        # Follow-ups due in the next 1-3 days, and missed ones 5-40 days ago with no later visit.
        due_soon = Visit.objects.filter(clinic=self.demo, follow_up_date__range=(today + timedelta(days=1), today + timedelta(days=3)))
        self.assertGreaterEqual(due_soon.count(), 3)
        for reminder in pending.filter(kind=Reminder.Kind.OVERDUE).select_related("visit"):
            days_late = (today - reminder.visit.follow_up_date).days
            self.assertTrue(5 <= days_late <= 40, days_late)
            self.assertFalse(Visit.objects.filter(patient=reminder.patient, visit_date__gt=reminder.visit.visit_date).exists())
        self.assertTrue(Reminder.objects.filter(clinic=self.demo, status=Reminder.Status.SENT).exists())

    def test_every_demo_login_opens_the_dashboard(self):
        for email in DEMO_EMAILS:
            with self.subTest(email=email):
                self.client.force_login(User.objects.get(email=email))
                response = self.client.get(reverse("core:dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["stats"]["waiting"], 2)
                self.assertGreater(response.context["stats"]["missed_follow_ups"], 0)
                self.assertGreaterEqual(len(response.context["asking_new_time"]), 1)

    def test_running_again_without_reset_is_refused(self):
        with self.assertRaisesMessage(CommandError, "--reset"):
            seed()
        self.assertEqual(Clinic.objects.filter(slug=DEMO_SLUG).count(), 1)


class SeedDemoClockTests(CoreTestCase):
    """Whenever the demo is seeded, nothing has happened in the future and nothing to come is overdue."""

    def check_timeline(self, now):
        demo = Clinic.objects.get(slug=DEMO_SLUG)
        appointments = Appointment.objects.filter(clinic=demo)
        self.assertFalse(Visit.objects.filter(clinic=demo, visit_date__gt=now).exists(), "a visit in the future")
        self.assertFalse(
            appointments.filter(status__in=[Status.COMPLETED, Status.NO_SHOW], scheduled_at__gt=now).exists(),
            "seen / did not come before the appointment time",
        )
        self.assertFalse(appointments.filter(status=Status.ARRIVED, updated_at__gt=now).exists(), "arrived in the future")
        self.assertEqual(appointments.filter(status=Status.ARRIVED).count(), 2)
        today_start = datetime.combine(timezone.localdate(now, KARACHI), time.min, tzinfo=KARACHI)
        self.assertFalse(
            appointments.filter(
                status__in=[Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED],
                scheduled_at__gte=today_start, scheduled_at__lt=now,
            ).exists(),
            "an appointment still marked as to come, though its time has passed",
        )
        self.assertFalse(Patient.objects.filter(clinic=demo, created_at__gt=now).exists(), "registered in the future")
        for appointment in appointments.filter(status=Status.COMPLETED, scheduled_at__gte=today_start):
            self.assertTrue(appointment.visits.exists(), "a 'Seen' appointment has its visit")
        return appointments.filter(scheduled_at__gte=today_start, scheduled_at__lt=today_start + timedelta(days=1))

    def test_early_morning_nobody_seen_yet(self):
        now = clinic_time_today(8, 5)
        seed_at(now, "--patients", "20")
        today = self.check_timeline(now)
        self.assertFalse(today.filter(status__in=[Status.COMPLETED, Status.NO_SHOW]).exists())

    def test_noon(self):
        now = clinic_time_today(12)
        seed_at(now, "--patients", "20")
        self.check_timeline(now)

    def test_afternoon(self):
        now = clinic_time_today(16)
        seed_at(now, "--patients", "20")
        today = self.check_timeline(now)
        self.assertTrue(today.filter(status=Status.COMPLETED).exists())
        self.assertTrue(today.filter(status=Status.SCHEDULED).exists() or today.filter(status=Status.CONFIRMED).exists())

    def test_late_evening_everyone_seen(self):
        now = clinic_time_today(22, 41)
        seed_at(now, "--patients", "20")
        today = self.check_timeline(now)
        self.assertFalse(today.filter(status__in=[Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED]).exists())


class SeedDemoResetTests(CoreTestCase):
    def setUp(self):
        super().setUp()
        # Real data in the two ordinary test clinics that seed_demo must never touch.
        patient = self.make_patient(full_name="Real Patient")
        self.make_visit(patient, diagnosis="Real diagnosis")
        self.make_appointment(patient)
        self.make_reminder(patient)
        self.make_patient(self.other_clinic, full_name="Other Real Patient")

    def test_seed_twice_with_reset_without_touching_other_clinics(self):
        before = other_clinics_snapshot()
        seed("--patients", "20")
        first_names = list(Patient.objects.filter(clinic__slug=DEMO_SLUG).order_by("mrn").values_list("full_name", flat=True))
        seed("--reset", "--patients", "20", "--password", "another-demo-pass-99")

        self.assertEqual(other_clinics_snapshot(), before)
        demo = Clinic.objects.get(slug=DEMO_SLUG)
        self.assertEqual(Patient.objects.filter(clinic=demo).count(), 20)
        self.assertEqual(User.objects.filter(email__in=DEMO_EMAILS).count(), 3)
        self.assertTrue(User.objects.get(email=DEMO_EMAILS[0]).check_password("another-demo-pass-99"))
        # Same seed, same day: the same patients again.
        second_names = list(Patient.objects.filter(clinic=demo).order_by("mrn").values_list("full_name", flat=True))
        self.assertEqual(first_names, second_names)

    def test_refuses_when_a_demo_email_works_in_another_clinic(self):
        make_user(self.clinic, Membership.Role.DOCTOR, email="demo-doctor@clinic.test", full_name="Real Doctor")
        before = other_clinics_snapshot()
        for args in ((), ("--reset",)):
            with self.subTest(args=args), self.assertRaisesMessage(CommandError, "another clinic"):
                seed(*args)
        self.assertFalse(Clinic.objects.filter(slug=DEMO_SLUG).exists())
        self.assertEqual(other_clinics_snapshot(), before)
        real_doctor = User.objects.get(email="demo-doctor@clinic.test")
        self.assertEqual(real_doctor.full_name, "Real Doctor")

    def test_refuses_to_reset_a_clinic_it_did_not_create(self):
        impostor = Clinic.objects.create(name="Demo Family Clinic", slug=DEMO_SLUG)
        make_user(impostor, Membership.Role.OWNER, email="someone@example.test")
        with self.assertRaisesMessage(CommandError, "not created by seed_demo"):
            seed("--reset")
        self.assertTrue(Clinic.objects.filter(pk=impostor.pk).exists())

    def test_bad_options(self):
        with self.assertRaisesMessage(CommandError, "--patients"):
            seed("--patients", "5")
        with self.assertRaisesMessage(CommandError, "too weak"):
            seed("--password", "123")
        self.assertFalse(Clinic.objects.filter(slug=DEMO_SLUG).exists())
