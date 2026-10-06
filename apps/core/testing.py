"""Factories for tests. Usage:

    from apps.core.testing import ClinicTestCase

    class MyTests(ClinicTestCase):
        def test_something(self):
            self.login(self.doctor)
            patient = self.make_patient()
            ...

ClinicTestCase.setUpTestData creates two clinics so every feature can prove
that clinic A can never see clinic B's data:
    self.clinic       owner/doctor/receptionist users: self.owner, self.doctor, self.receptionist
    self.other_clinic with self.other_owner
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Clinic, Membership, User

TEST_PASSWORD = "test-pass-12345"

# The real password hasher is slow on purpose (seconds per password on a laptop).
# Tests create several users per class, so they use a fast one. Tests only: the
# production setting in config/settings.py is never changed.
FAST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def make_clinic(name="Test Clinic", **kwargs):
    kwargs.setdefault("country", Clinic.Country.PAKISTAN)
    kwargs.setdefault("timezone", "Asia/Karachi")
    return Clinic.objects.create(name=name, **kwargs)


def make_user(clinic, role=Membership.Role.DOCTOR, email=None, full_name=None, **membership_kwargs):
    n = User.objects.count() + 1
    user = User.objects.create_user(
        email=email or f"user{n}@example.test",
        password=TEST_PASSWORD,
        full_name=full_name or f"Test User {n}",
    )
    if role == Membership.Role.DOCTOR or role == Membership.Role.OWNER:
        membership_kwargs.setdefault("title", "Dr.")
    Membership.objects.create(user=user, clinic=clinic, role=role, **membership_kwargs)
    return user


def make_patient(clinic, **kwargs):
    from apps.patients.models import Patient

    kwargs.setdefault("full_name", "Ali Raza")
    kwargs.setdefault("sex", Patient.Sex.MALE)
    kwargs.setdefault("phone", "0300-1234567")
    kwargs.setdefault("date_of_birth", timezone.localdate() - timedelta(days=365 * 40))
    return Patient.objects.create(clinic=clinic, **kwargs)


def make_visit(patient, doctor, **kwargs):
    from apps.clinical.models import Visit

    kwargs.setdefault("chief_complaint", "Fever for 3 days")
    return Visit.objects.create(clinic=patient.clinic, patient=patient, doctor=doctor, **kwargs)


def make_appointment(patient, doctor=None, when=None, **kwargs):
    from apps.appointments.models import Appointment

    when = when or timezone.now() + timedelta(days=1)
    return Appointment.objects.create(
        clinic=patient.clinic, patient=patient, doctor=doctor, scheduled_at=when, **kwargs
    )


@override_settings(PASSWORD_HASHERS=FAST_PASSWORD_HASHERS)
class ClinicTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.clinic = make_clinic("Al-Noor Family Clinic")
        cls.owner = make_user(cls.clinic, Membership.Role.OWNER, email="owner@example.test", full_name="Sara Ahmed")
        cls.doctor = make_user(cls.clinic, Membership.Role.DOCTOR, email="doctor@example.test", full_name="Bilal Hussain")
        cls.receptionist = make_user(
            cls.clinic, Membership.Role.RECEPTIONIST, email="reception@example.test", full_name="Hina Malik"
        )
        cls.other_clinic = make_clinic("Other Clinic")
        cls.other_owner = make_user(
            cls.other_clinic, Membership.Role.OWNER, email="other@example.test", full_name="Omar Farooq"
        )

    def login(self, user):
        self.client.force_login(user)

    def make_patient(self, clinic=None, **kwargs):
        return make_patient(clinic or self.clinic, **kwargs)

    def make_visit(self, patient, doctor=None, **kwargs):
        return make_visit(patient, doctor or self.doctor, **kwargs)

    def make_appointment(self, patient, doctor=None, when=None, **kwargs):
        return make_appointment(patient, doctor or self.doctor, when, **kwargs)
