"""Create a demo clinic full of realistic, made-up patients.

    .venv/Scripts/python manage.py seed_demo             # first time
    .venv/Scripts/python manage.py seed_demo --reset     # start again with fresh data, e.g. before a demo
    .venv/Scripts/python manage.py seed_demo --patients 120 --password "another-demo-pass"

What it creates
    "Demo Family Clinic" (Lahore) with three logins - owner Dr. Sara Ahmed, doctor Dr. Bilal Hussain
    and receptionist Hina Malik - and a year of history: patients, visits with vitals and
    prescriptions, lab results, appointments (today, tomorrow and the next two weeks) and the WhatsApp
    reminders that go with them. All dates are relative to today, so the demo always looks current:
    people in the waiting room, follow-ups due in the next few days, some patients who never came back.

Safety
    * PHONE NUMBERS ARE FAKE AND UNALLOCATED. Every patient number starts with 0390-, a prefix that
      no Pakistani mobile network uses, so tapping "Send on WhatsApp" during a demo can never message
      a real stranger. Please keep it that way if you change this file.
    * Only the demo clinic and its three demo users are created, changed or deleted. The command
      refuses to run if a demo email address is used in another clinic, or if a clinic it did not
      create already has the demo clinic's web address (slug).
    * NEVER RUN IT ON A DATABASE WITH REAL PATIENTS. The demo logins and their password are public
      (they are printed here and in the README), so anyone could sign in to the demo clinic.
    * Deterministic: the same random seed (42) gives the same patients every time for a given day.
      (Appointment confirmation links stay random: they are secrets.)
"""

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Clinic, Membership, User
from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem, Visit
from apps.core.audit import Action, log_action
from apps.core.management import demo_data as data
from apps.patients.models import Patient
from apps.reminders.models import Reminder
from apps.reminders.services import MessageBuilder, generate_reminders

SEED = 42
DEFAULT_PASSWORD = "demo-pass-2026"
DEFAULT_PATIENTS = 60
MIN_PATIENTS, MAX_PATIENTS = 20, 1000

DEMO_SLUG = "demo-family-clinic"
DEMO_PHONE_PREFIX = "0390"  # not used by any Pakistani mobile network: see the module docstring
CLINIC_DETAILS = {
    "name": "Demo Family Clinic",
    "country": Clinic.Country.PAKISTAN,
    "timezone": "Asia/Karachi",
    "address": "Plot 12-B, Main Boulevard, Model Town",
    "city": "Lahore",
    "phone": f"{DEMO_PHONE_PREFIX}-0001234",
    "email": "demo-clinic@clinic.test",
    # Timings only: the prescription prints the clinic's phone number on its own line already.
    "prescription_header": (
        "Mon to Sat: 10:00 am - 1:00 pm and 5:00 pm - 9:00 pm\n"
        "Sunday: closed"
    ),
    "prescription_footer": (
        "Please bring this prescription to your next visit. In an emergency, go straight to the nearest "
        "hospital emergency department.\n"
        "Demo clinic with made-up patients: not a valid prescription."
    ),
}
STAFF = [
    {
        "key": "owner", "email": "demo-owner@clinic.test", "full_name": "Sara Ahmed",
        "role": Membership.Role.OWNER, "title": "Dr.", "qualifications": "MBBS, FCPS (Medicine)",
        "registration_number": "00000-P (demo)",
    },
    {
        "key": "doctor", "email": "demo-doctor@clinic.test", "full_name": "Bilal Hussain",
        "role": Membership.Role.DOCTOR, "title": "Dr.", "qualifications": "MBBS, MCPS (Family Medicine)",
        "registration_number": "00001-P (demo)",
    },
    {
        "key": "reception", "email": "demo-reception@clinic.test", "full_name": "Hina Malik",
        "role": Membership.Role.RECEPTIONIST, "title": "", "qualifications": "", "registration_number": "",
    },
]
DEMO_EMAILS = [person["email"] for person in STAFF]

# What the demo needs each patient's latest visit to show.
REGULAR = "regular"  # follow-ups done (or none needed)
NEW = "new"  # registered this month
DUE_SOON = "due_soon"  # follow-up due in the next 1-3 days
OVERDUE = "overdue"  # follow-up 5-40 days ago and never came back

Status = Appointment.Status
CLINIC_SLOTS = [(hour, minute) for hour in (10, 11, 12, 17, 18, 19, 20) for minute in (0, 15, 30, 45)]


# --- Plans: what to create, worked out before anything is saved -------------------------------

@dataclass
class PlannedPatient:
    full_name: str
    sex: str
    age: int  # true age in years (0 = a baby), used for doses and vitals even when the DOB is unknown
    date_of_birth: date | None
    dob_is_estimated: bool
    guardian_name: str
    phone: str
    whatsapp_phone: str
    address: str
    city: str
    blood_group: str
    allergies: list
    conditions: list
    notes: str
    weight: float
    height: float
    usual_doctor: int  # 0 = Dr. Sara, 1 = Dr. Bilal
    bp_medicine: str
    on_getryl: bool
    reminders_opt_in: bool = True
    no_phone: bool = False
    group: str = REGULAR
    target_follow_up: date | None = None  # the date their latest visit asked them to come back
    book_follow_up: bool = False  # already booked an appointment for that date
    has_upcoming: bool = False
    registered: date | None = None
    visits: list = field(default_factory=list)
    obj: Patient | None = None


@dataclass
class PlannedVisit:
    when: datetime
    encounter: str
    doctor: User
    follow_up_date: date | None = None
    appointment: "PlannedAppointment | None" = None  # today's "Seen" appointments


@dataclass
class PlannedAppointment:
    patient: PlannedPatient
    when: datetime
    doctor: User | None
    status: str
    reason: str
    patient_note: str = ""
    responded_at: datetime | None = None
    arrived_at: datetime | None = None
    reminder_sent_at: datetime | None = None  # the reminder for it was already sent
    obj: Appointment | None = None


def years_before(day, years):
    """The same calendar day `years` earlier (29 Feb becomes 28 Feb)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


# --- The command ---------------------------------------------------------------------------------

class Command(BaseCommand):
    help = "Create the demo clinic with realistic made-up patients (--reset to start again)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true",
            help="Delete the demo clinic and its demo users first, then create them again.",
        )
        parser.add_argument(
            "--password", default=DEFAULT_PASSWORD,
            help=f'Password for the three demo logins (default "{DEFAULT_PASSWORD}").',
        )
        parser.add_argument(
            "--patients", type=int, default=DEFAULT_PATIENTS,
            help=f"How many patients to create ({MIN_PATIENTS}-{MAX_PATIENTS}, default {DEFAULT_PATIENTS}).",
        )

    def handle(self, *args, reset=False, password=DEFAULT_PASSWORD, patients=DEFAULT_PATIENTS, **options):
        if not MIN_PATIENTS <= patients <= MAX_PATIENTS:
            raise CommandError(f"--patients must be between {MIN_PATIENTS} and {MAX_PATIENTS}.")
        try:
            validate_password(password)
        except ValidationError as error:
            raise CommandError("That password is too weak: " + " ".join(error.messages)) from error

        with transaction.atomic():
            demo_clinic = self.find_demo_clinic()
            self.check_demo_users_are_free(demo_clinic)
            if demo_clinic is not None:
                if not reset:
                    raise CommandError(
                        "The demo clinic already exists. Run `manage.py seed_demo --reset` to delete it "
                        "and create a fresh copy."
                    )
                demo_clinic.delete()  # patients, visits, appointments, reminders, audit entries...
            if reset:
                User.objects.filter(email__in=DEMO_EMAILS).delete()

            builder = DemoBuilder(patient_count=patients, password=password)
            summary = builder.build()

        self.print_summary(summary, password)

    # Safety checks: never touch anything outside the demo clinic.

    def find_demo_clinic(self):
        clinic = Clinic.objects.filter(slug=DEMO_SLUG).first()
        if clinic is None:
            return None
        created_by_us = clinic.memberships.filter(
            user__email=STAFF[0]["email"], role=Membership.Role.OWNER
        ).exists()
        if not created_by_us:
            raise CommandError(
                f'A clinic with the address "{DEMO_SLUG}" exists but was not created by seed_demo '
                f"(its owner is not {STAFF[0]['email']}). Nothing was changed."
            )
        return clinic

    def check_demo_users_are_free(self, demo_clinic):
        """A demo login that also works in a real clinic must never be changed or deleted."""
        for email in DEMO_EMAILS:
            user = User.objects.filter(email=email).first()
            if user is None:
                continue
            memberships = user.memberships.all()
            visits = Visit.objects.filter(doctor=user)
            if demo_clinic is not None:
                memberships = memberships.exclude(clinic=demo_clinic)
                visits = visits.exclude(clinic=demo_clinic)
            if memberships.exists() or visits.exists():
                raise CommandError(
                    f"{email} is also used in another clinic, so seed_demo will not change it. "
                    "Nothing was changed."
                )

    def print_summary(self, summary, password):
        write = self.stdout.write
        write(self.style.SUCCESS(f"Demo clinic ready: {summary['clinic']}"))
        write(
            f"  {summary['patients']} patients, {summary['visits']} visits, "
            f"{summary['prescription_lines']} prescription lines, {summary['lab_results']} lab results"
        )
        write(
            f"  Appointments: {summary['today']} today ({summary['waiting']} waiting), "
            f"{summary['tomorrow']} tomorrow, {summary['later']} more in the next two weeks"
        )
        write(
            f"  Reminders to send today: {summary['reminders_due']} "
            f"(including {summary['missed']} missed follow-ups)"
        )
        write("")
        write(f"Sign in at {settings.SITE_URL}/ with any of these logins (password: {password})")
        for person in STAFF:
            role = Membership.Role(person["role"]).label
            name = f"{person['title']} {person['full_name']}".strip()
            write(f"  {person['email']:<28} {name} ({role.lower()})")
        write("")
        write(
            f"All patient phone numbers start with {DEMO_PHONE_PREFIX}- (not used by any network), "
            "so WhatsApp buttons never reach a real person."
        )


class DemoBuilder:
    """Works out the demo clinic's story (who, when, what) and then saves it."""

    def __init__(self, *, patient_count, password):
        self.rng = random.Random(SEED)
        self.patient_count = patient_count
        self.password = password
        self.tz = ZoneInfo(CLINIC_DETAILS["timezone"])
        self.now = timezone.localtime(timezone.now(), self.tz)
        self.today = self.now.date()
        self.yesterday = self.today - timedelta(days=1)
        self.month_start = self.today.replace(day=1)
        self.earliest = self.today - timedelta(days=360)  # visits cover the past 12 months
        self.counts = Counter()
        self.past_appointments = []
        self.first_hba1c_done = False

    # --- Small helpers ------------------------------------------------------------------------

    def at(self, day, hour, minute=0):
        return datetime.combine(day, time(hour, minute), tzinfo=self.tz)

    def clinic_time(self, day):
        """A consultation time on `day` during clinic hours (10 am - 1 pm or 5 pm - 9 pm)."""
        hour = self.rng.randint(10, 12) if self.rng.random() < 0.45 else self.rng.randint(17, 20)
        return self.at(day, hour, self.rng.randrange(0, 60, 5))

    def free_slots(self, day, count):
        return [self.at(day, hour, minute) for hour, minute in sorted(self.rng.sample(CLINIC_SLOTS, count))]

    def doctor_for(self, patient):
        """Usually the patient's own doctor, sometimes the other one."""
        index = patient.usual_doctor if self.rng.random() < 0.8 else 1 - patient.usual_doctor
        return self.doctors[index]

    def follow_up_after(self, encounter_key, day):
        days = data.ENCOUNTERS[encounter_key].follow_up_days
        return day + timedelta(days=self.rng.randint(*days)) if days else None

    def acute_encounter(self, patient, needs_follow_up=False):
        choices = data.acute_choices(patient.age, patient.sex)
        if needs_follow_up:
            choices = [key for key in choices if data.ENCOUNTERS[key].follow_up_days] or choices
        return self.rng.choice(choices)

    def review_encounter(self, patient):
        return data.REVIEW_FOR[self.rng.choice(patient.conditions)]

    # --- Build ------------------------------------------------------------------------------------

    def build(self):
        self.create_clinic_and_staff()
        patients = self.plan_patients()
        self.assign_groups(patients)
        appointments = self.plan_appointments(patients)
        for patient in patients:
            self.plan_visits(patient)

        self.save_patients(patients)
        self.save_appointments(appointments)
        self.save_visits(patients)
        self.save_missed_appointments(patients)
        self.save_sent_reminders(appointments)

        generate_reminders(self.clinic)
        log_action(None, Action.CREATE, self.clinic, "Demo clinic created with sample data", clinic=self.clinic)
        return self.summary(patients, appointments)

    def create_clinic_and_staff(self):
        self.clinic = Clinic.objects.create(slug=DEMO_SLUG, **CLINIC_DETAILS)
        users = {}
        for person in STAFF:
            user = User.objects.filter(email=person["email"]).first()
            if user is None:
                user = User.objects.create_user(
                    email=person["email"], password=self.password, full_name=person["full_name"]
                )
            else:  # left over from an earlier run (checked: not used by any other clinic)
                user.full_name = person["full_name"]
                user.is_active = True
                user.set_password(self.password)
                user.save()
            Membership.objects.create(
                user=user, clinic=self.clinic, role=person["role"], title=person["title"],
                qualifications=person["qualifications"], registration_number=person["registration_number"],
            )
            users[person["key"]] = user
        self.doctors = [users["owner"], users["doctor"]]
        self.receptionist = users["reception"]

    # --- Patients -----------------------------------------------------------------------------

    def random_age(self):
        """(years, months) with a realistic mix: children, adults and elderly patients."""
        rng, roll = self.rng, self.rng.random()
        if roll < 0.04:
            return 0, rng.randint(4, 11)
        if roll < 0.15:
            return rng.randint(1, 12), 0
        if roll < 0.22:
            return rng.randint(13, 19), 0
        if roll < 0.50:
            return rng.randint(20, 39), 0
        if roll < 0.85:
            return rng.randint(40, 64), 0
        return rng.randint(65, 86), 0

    def fake_phone(self, used):
        while True:
            number = f"{DEMO_PHONE_PREFIX}-{self.rng.randint(1000000, 9999999)}"
            if number not in used:
                used.add(number)
                return number

    def plan_patients(self):
        rng, today = self.rng, self.today
        used_names, used_phones = set(), set()
        patients = []
        for _ in range(self.patient_count):
            sex = "F" if rng.random() < 0.52 else "M"
            age, months = self.random_age()
            generation = "child" if age < 13 else ("elder" if age >= 60 else "adult")
            first_names = (data.FEMALE_NAMES if sex == "F" else data.MALE_NAMES)[generation]
            for _attempt in range(50):
                surname = rng.choice(data.SURNAMES)
                full_name = f"{rng.choice(first_names)} {surname}"
                if full_name not in used_names:
                    break
            used_names.add(full_name)

            # Date of birth: exact, worked out from an age ("about 60"), or not known at all.
            if age == 0:
                dob = today - timedelta(days=months * 30 + rng.randint(0, 25))
            else:
                dob = today - timedelta(days=age * 365 + age // 4 + rng.randint(10, 350))
            estimated = False
            if age >= 20 and rng.random() < 0.05:
                dob = None
            elif age >= 35 and rng.random() < 0.3:
                dob, estimated = years_before(today - timedelta(days=rng.randint(30, 300)), age), True

            conditions = self.random_conditions(age, sex)
            if age == 0:
                weight, height = 6.5 + months * 0.45, 62 + months * 1.6
            elif age < 13:
                weight, height = 2 * age + 8 + rng.uniform(-1.5, 2), 77 + 6 * age + rng.uniform(-3, 3)
            elif age < 18:
                weight, height = 40 + (age - 13) * 4 + rng.uniform(-3, 3), 148 + (age - 13) * 4 + rng.uniform(-3, 3)
            elif sex == "M":
                weight, height = rng.uniform(60, 90), rng.uniform(162, 182)
            else:
                weight, height = rng.uniform(50, 80), rng.uniform(150, 167)
            if data.DIABETES in conditions or data.HYPERTENSION in conditions:
                weight += 6

            address, city = self.random_address()
            patients.append(PlannedPatient(
                full_name=full_name,
                sex=sex,
                age=age,
                date_of_birth=dob,
                dob_is_estimated=estimated,
                guardian_name=self.guardian_name(age, surname),
                phone=self.fake_phone(used_phones),
                whatsapp_phone=self.fake_phone(used_phones) if rng.random() < 0.06 else "",
                address=address,
                city=city,
                blood_group=self.random_blood_group(),
                allergies=[rng.choice(data.ALLERGY_CHOICES)] if rng.random() < 0.15 else [],
                conditions=conditions,
                notes=rng.choice(data.FRONT_DESK_NOTES) if rng.random() < 0.1 else "",
                weight=weight,
                height=height,
                usual_doctor=0 if rng.random() < 0.55 else 1,
                bp_medicine=rng.choice(["amlodipine", "losartan"]),
                on_getryl=rng.random() < 0.5,
            ))

        self.make_sure_every_example_appears(patients)
        # Allergies are stored as text ("Penicillin, NSAIDs"); keep the parts for prescribing.
        for patient in patients:
            patient.allergies = [part.strip() for item in patient.allergies for part in item.split(",")]
        return patients

    def random_address(self):
        """(address, city): mostly Lahore, some from towns around it."""
        rng = self.rng
        if rng.random() < 0.75:
            street = f"House {rng.randint(1, 250)}, Street {rng.randint(1, 30)}"
            return f"{street}, {rng.choice(data.LAHORE_AREAS)}", "Lahore"
        return f"Mohallah {rng.choice(['Islampura', 'Rehmanpura', 'Madina Town'])}", rng.choice(data.NEARBY_TOWNS)

    def random_blood_group(self):
        """Blank for about 3 in 10 patients (not known); otherwise roughly Pakistan's mix."""
        if self.rng.random() >= 0.7:
            return ""
        groups, weights = ["O+", "B+", "A+", "AB+", "O-", "B-", "A-", "AB-"], [30, 28, 21, 7, 5, 4, 3, 2]
        return self.rng.choices(groups, weights=weights)[0]

    def guardian_name(self, age, surname):
        """Father's (or husband's) name; some adults leave it blank."""
        if age >= 18 and self.rng.random() < 0.15:
            return ""
        return f"{self.rng.choice(data.GUARDIAN_FIRST_NAMES)} {surname}"

    def random_conditions(self, age, sex):
        rng, conditions = self.rng, []
        if age >= 35:
            if rng.random() < (0.38 if age >= 55 else 0.25):
                conditions.append(data.DIABETES)
            if rng.random() < (0.45 if age >= 55 else 0.28):
                conditions.append(data.HYPERTENSION)
            if rng.random() < (0.14 if sex == "F" else 0.03):
                conditions.append(data.HYPOTHYROIDISM)
            if age >= 50 and rng.random() < (0.12 if sex == "M" else 0.06):
                conditions.append(data.IHD)
        if age >= 4 and rng.random() < 0.07:
            conditions.append(data.ASTHMA)
        return conditions

    def make_sure_every_example_appears(self, patients):
        """Every long-term condition and allergy the demo talks about appears at least once."""
        rules = [
            (data.DIABETES, lambda p: p.age >= 40),
            (data.HYPERTENSION, lambda p: p.age >= 40),
            (data.ASTHMA, lambda p: p.age >= 5),
            (data.HYPOTHYROIDISM, lambda p: p.age >= 25 and p.sex == "F"),
            (data.IHD, lambda p: p.age >= 50),
        ]
        for condition, suits in rules:
            if not any(condition in p.conditions for p in patients):
                candidates = [p for p in patients if suits(p)]
                if candidates:
                    self.rng.choice(candidates).conditions.append(condition)
        for allergy in (data.PENICILLIN, data.SULFA, data.NSAIDS):
            if not any(allergy in item for p in patients for item in p.allergies):
                candidates = [p for p in patients if p.age >= 18 and not p.allergies]
                if candidates:
                    self.rng.choice(candidates).allergies = [allergy]

    def assign_groups(self, patients):
        rng, n = self.rng, len(patients)
        order = patients[:]
        rng.shuffle(order)
        overdue_count, due_count = max(2, round(n * 8 / 60)), max(2, round(n * 6 / 60))
        new_count, opted_out_count = max(1, round(n * 5 / 60)), max(1, round(n * 0.1))

        # Follow-up stories are about adults (children's illnesses rarely need one).
        adults_first = [p for p in order if p.age >= 18] + [p for p in order if p.age < 18]
        for patient in adults_first[:overdue_count]:
            patient.group = OVERDUE
            patient.target_follow_up = self.today - timedelta(days=rng.randint(5, 40))
        for index, patient in enumerate(adults_first[overdue_count:overdue_count + due_count]):
            patient.group = DUE_SOON
            if index < 2:  # two of them have already booked an appointment for that day
                patient.target_follow_up = self.today + timedelta(days=rng.choice((2, 3)))
                patient.book_follow_up = True
            else:
                patient.target_follow_up = self.today + timedelta(days=rng.randint(1, 3))

        regular = [p for p in order if p.group == REGULAR]
        for patient in regular[:new_count]:
            patient.group = NEW
        regular = regular[new_count:]
        for patient in regular[:opted_out_count]:
            patient.reminders_opt_in = False  # about 1 in 10 patients do not want WhatsApp reminders
        # One elderly patient has no mobile number, to show how the app copes with that.
        for patient in regular[opted_out_count:]:
            if patient.age >= 60:
                patient.phone, patient.whatsapp_phone, patient.no_phone = "", "", True
                patient.notes = "No mobile number. Ask the son for a contact number when they visit."
                break

    # --- Appointments: today, tomorrow and the next two weeks ----------------------------------

    def plan_appointments(self, patients):
        pool = [p for p in patients if p.group in (REGULAR, NEW) and not p.no_phone]
        self.rng.shuffle(pool)
        no_phone = [p for p in patients if p.no_phone]

        def take(count):
            chosen = [pool.pop() for _ in range(min(count, len(pool)))]
            for patient in chosen:
                patient.has_upcoming = True
            return chosen

        planned = self.plan_today(take)
        planned += self.plan_tomorrow(take, no_phone)
        planned += self.plan_next_two_weeks(take, patients)
        return planned

    def booking_reason(self, patient, day, same_week):
        """(encounter key, reason) for a booking. Follow-up bookings also set the patient's follow-up date."""
        if patient.group != NEW and patient.conditions and self.rng.random() < 0.6:
            key = self.review_encounter(patient)
            patient.target_follow_up = day
            return key, data.ENCOUNTERS[key].reason
        if same_week:
            key = self.acute_encounter(patient)
            return key, data.ENCOUNTERS[key].reason
        return "", "General check-up"

    def todays_slots(self):
        """10-12 appointment times today, spread around the current time but inside clinic hours."""
        anchor = self.at(self.today, self.now.hour, self.now.minute - self.now.minute % 15)
        anchor = min(max(anchor, self.at(self.today, 13)), self.at(self.today, 19))
        offsets = [-180, -150, -135, -120, -30, -15, 15, 45, 60, 90, 120, 150]
        offsets = offsets[: len(offsets) - self.rng.randint(0, 2)]
        return [anchor + timedelta(minutes=offset) for offset in offsets]

    def todays_statuses(self, slots):
        """What happened to each of today's appointments, decided by comparing its time with now.

        Two patients are in the waiting room, booked for the slots just before now (the doctors run a
        little late); in the morning, before any slot has passed, the first two slots instead (early).
        Earlier slots are over: seen, except one who did not come. Later slots are still to come:
        booked or confirmed, and one patient wants another time. So run at 8 am nobody has been seen
        yet, and run late in the evening everyone has.
        """
        past = [when for when in slots if when < self.now]
        coming = [when for when in slots if when >= self.now]
        waiting = past[-2:] if len(past) >= 2 else past + coming[: 2 - len(past)]
        done = [when for when in past if when not in waiting]
        to_come = [when for when in coming if when not in waiting]

        done_story = [Status.COMPLETED, Status.COMPLETED, Status.NO_SHOW]  # then everyone else was seen
        coming_story = [Status.CONFIRMED, Status.SCHEDULED, Status.RESCHEDULE_REQUESTED, Status.CONFIRMED]
        statuses = {when: Status.ARRIVED for when in waiting}
        for index, when in enumerate(done):
            statuses[when] = done_story[index] if index < len(done_story) else Status.COMPLETED
        for index, when in enumerate(to_come):
            statuses[when] = coming_story[index] if index < len(coming_story) else Status.SCHEDULED
        return [statuses[when] for when in slots]

    def plan_today(self, take):
        """10-12 appointments around now: two waiting, earlier ones over, later ones still to come."""
        rng, today = self.rng, self.today
        slots = self.todays_slots()
        planned, arrivals = [], 0
        for index, (when, status) in enumerate(zip(slots, self.todays_statuses(slots))):
            chosen = take(1)
            if not chosen:
                break
            patient = chosen[0]
            encounter, reason = self.booking_reason(patient, today, same_week=True)
            appointment = PlannedAppointment(
                patient=patient, when=when, doctor=self.doctors[index % 2], status=status, reason=reason
            )
            if patient.reminders_opt_in:  # reminders went out yesterday afternoon
                appointment.reminder_sent_at = self.at(self.yesterday, 16, rng.randrange(0, 60, 5))
            if status == Status.CONFIRMED:
                appointment.responded_at = self.at(self.yesterday, rng.randint(18, 21), rng.randrange(0, 60, 5))
            elif status == Status.RESCHEDULE_REQUESTED:
                appointment.responded_at = self.at(self.yesterday, 20, rng.randrange(0, 60, 5))
                appointment.patient_note = "Can I come tomorrow evening instead? I am stuck at work."
            elif status == Status.ARRIVED:
                # Arrival order = token order on the dashboard. Never in the future, never before today.
                arrivals += 1
                arrived = min(when - timedelta(minutes=5), self.now - timedelta(minutes=21 - 7 * arrivals))
                appointment.arrived_at = min(max(arrived, self.at(today, 0)), self.now)
            elif status == Status.COMPLETED:
                follow_up = self.follow_up_after(encounter, today)
                seen_at = min(when + timedelta(minutes=rng.randint(5, 12)), self.now - timedelta(minutes=1))
                # They sat in the waiting room first, so they hold the earlier token numbers.
                arrived = min(when - timedelta(minutes=5), seen_at - timedelta(minutes=10))
                appointment.arrived_at = min(max(arrived, self.at(today, 0)), seen_at)
                patient.visits.append(PlannedVisit(
                    when=seen_at, encounter=encounter,
                    doctor=appointment.doctor, follow_up_date=follow_up, appointment=appointment,
                ))
            planned.append(appointment)
        return planned

    def plan_tomorrow(self, take, no_phone):
        rng, tomorrow = self.rng, self.today + timedelta(days=1)
        count = rng.randint(6, 8)
        # The patient without a mobile number is booked first, so the dashboard shows a reminder
        # that can't go on WhatsApp.
        patients = no_phone[:1] + take(count - len(no_phone[:1]))
        for patient in no_phone[:1]:
            patient.has_upcoming = True
        planned = []
        for index, (patient, when) in enumerate(zip(patients, self.free_slots(tomorrow, len(patients)))):
            _, reason = self.booking_reason(patient, tomorrow, same_week=True)
            doctor = None if index == 3 else self.doctors[index % 2]  # "any available doctor"
            appointment = PlannedAppointment(patient=patient, when=when, doctor=doctor, status=Status.SCHEDULED, reason=reason)
            if patient.reminders_opt_in and not patient.no_phone and rng.random() < 0.4:
                # Reminder already sent this morning and the patient tapped "Confirm".
                appointment.status = Status.CONFIRMED
                appointment.responded_at = self.now - timedelta(minutes=rng.randint(20, 120))
                appointment.reminder_sent_at = appointment.responded_at - timedelta(minutes=rng.randint(10, 60))
            planned.append(appointment)

        # Another one tapped "I need another time": the Today page asks staff to call them back,
        # whatever time of day the demo runs.
        asking = next(
            (a for a in planned if a.status == Status.SCHEDULED and a.patient.reminders_opt_in and not a.patient.no_phone),
            None,
        )
        if asking is not None:
            asking.status = Status.RESCHEDULE_REQUESTED
            asking.responded_at = self.now - timedelta(minutes=rng.randint(30, 90))
            asking.reminder_sent_at = asking.responded_at - timedelta(minutes=rng.randint(10, 60))
            asking.patient_note = "Could I come on Saturday morning instead?"
        return planned

    def plan_next_two_weeks(self, take, patients):
        rng, today, planned = self.rng, self.today, []
        for offset in range(2, 15):
            day = today + timedelta(days=offset)
            if day.weekday() == 6:  # closed on Sundays
                continue
            chosen = take(rng.randint(1, 3))
            for patient, when in zip(chosen, self.free_slots(day, len(chosen))):
                _, reason = self.booking_reason(patient, day, same_week=False)
                doctor = None if rng.random() < 0.1 else self.doctors[patient.usual_doctor]
                planned.append(PlannedAppointment(patient=patient, when=when, doctor=doctor, status=Status.SCHEDULED, reason=reason))

        # Patients who booked their follow-up in advance.
        for patient in patients:
            if patient.book_follow_up:
                patient.has_upcoming = True
                key = self.review_encounter(patient) if patient.conditions else ""
                planned.append(PlannedAppointment(
                    patient=patient, when=self.free_slots(patient.target_follow_up, 1)[0],
                    doctor=self.doctors[patient.usual_doctor], status=Status.SCHEDULED,
                    reason=data.ENCOUNTERS[key].reason if key else "Follow-up",
                ))

        # A couple of cancelled bookings.
        for patient in take(2):
            day = today + timedelta(days=rng.randint(3, 10))
            if day.weekday() == 6:
                day += timedelta(days=1)
            planned.append(PlannedAppointment(
                patient=patient, when=self.free_slots(day, 1)[0], doctor=self.doctors[patient.usual_doctor],
                status=Status.CANCELLED, reason="General check-up",
            ))
        return planned

    # --- Visits over the past year -------------------------------------------------------------

    def plan_visits(self, patient):
        rng, today = self.rng, self.today
        todays_visits = patient.visits  # a visit today, if they were already seen

        if patient.group == NEW:
            patient.registered = self.month_start + timedelta(days=rng.randint(0, (today - self.month_start).days))
            past = []
            if patient.registered < today:
                key = self.review_encounter(patient) if patient.conditions else self.acute_encounter(patient)
                follow_up = self.free_follow_up(key, patient.registered)
                past.append(PlannedVisit(self.clinic_time(patient.registered), key, self.doctor_for(patient), follow_up))
            patient.visits = past + todays_visits
            return

        # The latest visit decides what the dashboard shows for this patient.
        target = patient.target_follow_up
        if target:
            key = self.review_encounter(patient) if patient.conditions else self.acute_encounter(patient, needs_follow_up=True)
            gap = rng.randint(*(data.ENCOUNTERS[key].follow_up_days or (7, 10)))
            last_day = min(target - timedelta(days=gap), today - timedelta(days=1))
            last_follow_up = target
        elif patient.conditions:
            key = self.review_encounter(patient)
            last_day = today - timedelta(days=rng.randint(5, 25))
            last_follow_up = self.free_follow_up(key, last_day)
        else:
            key = self.acute_encounter(patient)
            last_day = today - timedelta(days=rng.randint(3, 330))
            last_follow_up = None  # "come back if needed"
        last_day = max(last_day, self.earliest)

        # Earlier visits, going back in time. Long-term patients come every month or two.
        visit_count = rng.randint(3, 6) if patient.conditions else rng.randint(1, 3)
        earlier_days, day = [], last_day
        for _ in range(visit_count - 1):
            day -= timedelta(days=rng.randint(25, 60) if patient.conditions else rng.randint(30, 140))
            if day < self.earliest:
                break
            earlier_days.insert(0, day)
        if (earlier_days[0] if earlier_days else last_day) >= self.month_start:
            # Not a new patient: their first visit was before this month.
            earlier_days.insert(0, self.month_start - timedelta(days=rng.randint(10, 200)))

        past = []
        for index, day in enumerate(earlier_days):
            next_day = earlier_days[index + 1] if index + 1 < len(earlier_days) else last_day
            if patient.conditions and rng.random() < 0.75:
                visit_key = self.review_encounter(patient)
                # They came back when asked (or a few days late): this follow-up is done.
                follow_up = max(next_day - timedelta(days=rng.randint(0, 3)), day + timedelta(days=7))
            else:
                visit_key = self.acute_encounter(patient)
                follow_up = self.follow_up_after(visit_key, day) if rng.random() < 0.5 else None
                if follow_up:
                    follow_up = min(follow_up, next_day)
            past.append(PlannedVisit(self.clinic_time(day), visit_key, self.doctor_for(patient), follow_up))
        past.append(PlannedVisit(self.clinic_time(last_day), key, self.doctor_for(patient), last_follow_up))

        patient.visits = past + todays_visits
        patient.registered = past[0].when.date()

    def free_follow_up(self, key, visit_day):
        """A follow-up date that lands after the "coming back soon" week (so it is neither due nor missed)."""
        follow_up = self.follow_up_after(key, visit_day)
        if follow_up is None:
            return None
        if follow_up <= self.today + timedelta(days=7):
            follow_up = self.today + timedelta(days=self.rng.randint(8, 21))
        return follow_up

    # --- Saving -------------------------------------------------------------------------------------

    def save_patients(self, patients):
        # Oldest first, so MR numbers go up with registration date like a real clinic's.
        for patient in sorted(patients, key=lambda p: p.registered):
            patient.obj = Patient.objects.create(
                clinic=self.clinic,
                full_name=patient.full_name,
                guardian_name=patient.guardian_name,
                sex=patient.sex,
                date_of_birth=patient.date_of_birth,
                dob_is_estimated=patient.dob_is_estimated,
                phone=patient.phone,
                whatsapp_phone=patient.whatsapp_phone,
                reminders_opt_in=patient.reminders_opt_in,
                address=patient.address,
                city=patient.city,
                blood_group=patient.blood_group,
                allergies=", ".join(patient.allergies),
                chronic_conditions=", ".join(patient.conditions),
                notes=patient.notes,
                created_by=self.receptionist if self.rng.random() < 0.8 else self.doctors[patient.usual_doctor],
            )
            # Registered on the day of their first visit (created_at is set automatically, so fix it after).
            first = patient.visits[0].when if patient.visits else self.at(patient.registered, 10)
            patient.obj.created_at = min(first - timedelta(minutes=15), self.now)
        Patient.objects.bulk_update([p.obj for p in patients], ["created_at"])
        self.counts["patients"] = len(patients)

    def save_appointments(self, planned):
        for appointment in planned:
            appointment.obj = Appointment.objects.create(
                clinic=self.clinic,
                patient=appointment.patient.obj,
                doctor=appointment.doctor,
                scheduled_at=appointment.when,
                duration_minutes=self.clinic.default_appointment_minutes,
                reason=appointment.reason,
                status=appointment.status,
                patient_note=appointment.patient_note,
                patient_responded_at=appointment.responded_at,
                # Waiting-room order and tokens (Appointment.save() keeps a time it is given).
                arrived_at=appointment.arrived_at,
                created_by=self.receptionist,
            )

    def save_visits(self, patients):
        rng = self.rng
        items, labs = [], []
        for patient in patients:
            for index, planned in enumerate(patient.visits):
                encounter = data.ENCOUNTERS[planned.encounter]
                appointment = planned.appointment.obj if planned.appointment else None
                if appointment is None and planned.when.date() < self.today and rng.random() < 0.45:
                    # Booked in advance (the rest were walk-ins).
                    appointment = self.past_appointment(
                        patient, planned.when, planned.doctor, encounter.reason, Status.COMPLETED
                    )

                plan_text = encounter.plan
                if planned.follow_up_date:
                    weeks = round((planned.follow_up_date - planned.when.date()).days / 7)
                    plan_text += f" Come back in {weeks} week{'s' if weeks != 1 else ''}." if weeks else " Come back in a few days."
                visit = Visit.objects.create(
                    clinic=self.clinic,
                    patient=patient.obj,
                    doctor=planned.doctor,
                    appointment=appointment,
                    visit_date=planned.when,
                    chief_complaint=data.pick(rng, encounter.complaint),
                    history=data.pick(rng, encounter.history),
                    examination=data.pick(rng, encounter.examination),
                    diagnosis=encounter.diagnosis,
                    plan=plan_text,
                    follow_up_date=planned.follow_up_date,
                    created_by=planned.doctor,
                    **data.vitals_for(
                        rng, age=patient.age, conditions=patient.conditions, weight=patient.weight,
                        height=patient.height, encounter=encounter, first_visit=index == 0,
                    ),
                )
                items += self.prescription(patient, planned.encounter, visit)
                labs += self.lab_results(patient, encounter, visit, planned.doctor)
                self.counts["visits"] += 1
        PrescriptionItem.objects.bulk_create(items)
        LabResult.objects.bulk_create(labs)
        self.counts["prescription_lines"], self.counts["lab_results"] = len(items), len(labs)

    def past_appointment(self, patient, when, doctor, reason, status):
        when = when.replace(minute=when.minute - when.minute % 15)  # bookings are on the quarter hour
        appointment = Appointment.objects.create(
            clinic=self.clinic, patient=patient.obj, doctor=doctor, scheduled_at=when,
            duration_minutes=self.clinic.default_appointment_minutes, reason=reason, status=status,
            created_by=self.receptionist,
        )
        self.past_appointments.append(appointment)
        return appointment

    def prescription(self, patient, encounter_key, visit):
        keys = list(data.ENCOUNTERS[encounter_key].medicines)
        if encounter_key == "hypertension_review":
            keys = [patient.bp_medicine]
        elif encounter_key == "diabetes_review" and not patient.on_getryl:
            keys.remove("getryl")
        elif encounter_key == "asthma_review" and patient.age < 12:
            keys = ["ventolin", "montika_child"]

        items = []
        for order, key in enumerate(data.medicines_for(keys, patient.allergies)):
            if key == "calpol" and patient.weight >= 30:
                key = "panadol"  # big enough for tablets
            medicine, dose, frequency, duration, instructions = data.MEDICINES[key]
            if key == "panadol" and patient.age < 18:
                dose = "1 tablet"
            items.append(PrescriptionItem(
                visit=visit, medicine=medicine, dose=dose or data.child_dose(key, patient.weight),
                frequency=frequency, duration=duration, instructions=instructions, order=order,
            ))
        return items

    def lab_results(self, patient, encounter, visit, doctor):
        results = []
        for lab_key, chance in encounter.labs:
            if self.rng.random() >= chance:
                continue
            test_name, options = data.LAB_TESTS[lab_key]
            if lab_key == "hba1c" and not self.first_hba1c_done:
                text, abnormal = options[0]  # make sure the demo has the clearly high HbA1c of 8.4%
                self.first_hba1c_done = True
            elif lab_key == "cbc":
                text, abnormal = options[0] if patient.sex == "F" else options[1]
            else:
                text, abnormal = self.rng.choice(options)
            result_day = min(visit.visit_date.astimezone(self.tz).date() + timedelta(days=self.rng.randint(0, 2)), self.today)
            results.append(LabResult(
                clinic=self.clinic, patient=patient.obj, visit=visit, test_name=test_name, result_date=result_day,
                result_text=text, is_abnormal=abnormal, uploaded_by=doctor,
            ))
        return results

    def save_missed_appointments(self, patients):
        """A few past bookings where the patient did not come, and two that were cancelled."""
        candidates = [p for p in patients if p.group == REGULAR and p.visits and not p.has_upcoming]
        chosen = self.rng.sample(candidates, min(6, len(candidates)))
        for index, patient in enumerate(chosen):
            day = max(self.today - timedelta(days=self.rng.randint(3, 60)), patient.registered + timedelta(days=1))
            if day >= self.today:
                continue
            status = Status.CANCELLED if index >= 4 else Status.NO_SHOW
            reason = "Follow-up" if patient.conditions else "General check-up"
            self.past_appointment(patient, self.clinic_time(day), self.doctors[patient.usual_doctor], reason, status)

    def save_sent_reminders(self, planned):
        """Appointment reminders staff already sent (yesterday for today's patients, and earlier ones)."""
        builder = MessageBuilder(self.clinic)
        sent = [(a.obj, a.reminder_sent_at) for a in planned if a.reminder_sent_at]
        two_weeks_ago = self.today - timedelta(days=14)
        for appointment in self.past_appointments:
            day = appointment.scheduled_at.astimezone(self.tz).date()
            if two_weeks_ago <= day < self.today and appointment.status != Status.CANCELLED:
                sent.append((appointment, self.at(day - timedelta(days=1), 17, self.rng.randrange(0, 60, 5))))

        reminders = []
        for appointment, sent_at in sent:
            if not appointment.patient.can_receive_whatsapp:
                continue
            reminders.append(Reminder(
                clinic=self.clinic, patient=appointment.patient, appointment=appointment,
                kind=Reminder.Kind.APPOINTMENT, due_date=sent_at.date(), message=builder.appointment_message(appointment),
                status=Reminder.Status.SENT, channel=Reminder.Channel.WHATSAPP_LINK, sent_at=sent_at,
                sent_by=self.receptionist,
            ))
        Reminder.objects.bulk_create(reminders)

    # --- Summary --------------------------------------------------------------------------------

    def summary(self, patients, planned):
        tomorrow = self.today + timedelta(days=1)
        active = [a for a in planned if a.status != Status.CANCELLED]
        pending_today = Reminder.objects.filter(
            clinic=self.clinic, status=Reminder.Status.PENDING, due_date__lte=self.today
        )
        return {
            "clinic": f"{self.clinic.name} ({self.clinic.city})",
            **self.counts,
            "today": sum(a.when.date() == self.today for a in active),
            "waiting": sum(a.status == Status.ARRIVED for a in active),
            "tomorrow": sum(a.when.date() == tomorrow for a in active),
            "later": sum(a.when.date() > tomorrow for a in active),
            "reminders_due": pending_today.count(),
            "missed": pending_today.filter(kind=Reminder.Kind.OVERDUE).count(),
        }
