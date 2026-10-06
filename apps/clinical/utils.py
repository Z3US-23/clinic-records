"""Small helpers shared by the clinical views, forms and templates."""

import calendar
from datetime import timedelta

from django.db.models import Count
from django.template.defaultfilters import floatformat

from apps.accounts.models import Membership

from .models import PrescriptionItem

# How many medicine names to offer as suggestions in the prescription rows.
MEDICINE_SUGGESTION_LIMIT = 300

# Quick picks next to the follow-up date: value -> label. "7d" = 7 days, "1m" = 1 month.
FOLLOW_UP_CHOICES = [
    ("7d", "1 week"),
    ("14d", "2 weeks"),
    ("1m", "1 month"),
    ("3m", "3 months"),
]

# Vitals in the order doctors read them: (label, Visit attribute, unit).
VITALS = [
    ("BP", "blood_pressure", "mmHg"),
    ("Pulse", "pulse", "bpm"),
    ("Temp", "temperature_c", "°C"),
    ("SpO₂", "spo2", "%"),
    ("Weight", "weight_kg", "kg"),
    ("Height", "height_cm", "cm"),
    ("BMI", "bmi", ""),
    ("Blood sugar", "blood_sugar", "mg/dL"),
]


# --- Permissions --------------------------------------------------------------

def can_edit_visit(request, visit):
    """Only the doctor who saw the patient, or a clinic owner, may change a visit."""
    membership = getattr(request, "membership", None)
    if membership is None or not membership.is_clinician:
        return False
    return membership.is_owner or visit.doctor_id == request.user.pk


# --- Doctors ------------------------------------------------------------------

def doctor_membership(clinic, user):
    """The doctor's Membership in this clinic (title, qualifications, registration no.), or None."""
    return Membership.objects.filter(clinic=clinic, user=user).select_related("user").first()


def doctor_display_name(clinic, user):
    """'Dr. Bilal Hussain' — the name as the clinic shows it."""
    membership = doctor_membership(clinic, user)
    return membership.display_name if membership else user.full_name


# --- Dates --------------------------------------------------------------------

def add_months(day, months):
    """The same day-of-month `months` later, kept inside short months (31 Jan + 1 month = 28/29 Feb)."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last_day))


def follow_up_from(day, choice):
    """Turn a quick pick ("7d", "14d", "1m", "3m") into a date counted from `day`."""
    amount, unit = int(choice[:-1]), choice[-1]
    if unit == "d":
        return day + timedelta(days=amount)
    return add_months(day, amount)


# --- Display ------------------------------------------------------------------

def vitals_for_display(visit):
    """Only the vitals that were filled in, as [{"label": "BP", "text": "120/80 mmHg"}, ...]."""
    shown = []
    for label, attribute, unit in VITALS:
        value = getattr(visit, attribute)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            value = floatformat(value, -1)  # 37.0 -> "37", 37.5 -> "37.5"
        text = f"{value}{unit}" if unit == "%" else f"{value} {unit}".strip()
        shown.append({"label": label, "text": text})
    return shown


def medicine_suggestions(clinic, limit=MEDICINE_SUGGESTION_LIMIT):
    """Names of medicines this clinic has prescribed before (most used first, then sorted A–Z)."""
    rows = (
        PrescriptionItem.objects.filter(visit__clinic=clinic)
        .values("medicine")
        .annotate(times=Count("id"))
        .order_by("-times", "medicine")[:limit]
    )
    return sorted({row["medicine"] for row in rows}, key=str.lower)
