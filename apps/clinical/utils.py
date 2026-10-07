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

# Printed prescription: paper sizes (CSS classes rx-a5 / rx-a4 in clinical.css) ...
PRESCRIPTION_PAPERS = {"a5": "A5 (half sheet)", "a4": "A4 (full sheet)"}
DEFAULT_PAPER = "a5"
# ... and, for pads with the letterhead already printed on them, the blank space (mm) left at the top.
PAD_SPACES = {30: "3 cm", 40: "4 cm", 50: "5 cm", 60: "6 cm"}


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
        if attribute == "temperature_c":
            value = floatformat(round(value, 1), -1)  # stored to 0.01 °C: 38.33 -> "38.3", 37.00 -> "37"
        elif not isinstance(value, str):
            value = floatformat(value, -1)  # 37.0 -> "37", 37.5 -> "37.5"
        text = f"{value}{unit}" if unit == "%" else f"{value} {unit}".strip()
        if attribute == "temperature_c":
            # Stored in °C; most doctors here read °F, so show both.
            text += f" ({floatformat(visit.temperature_f, 1)} °F)"
        shown.append({"label": label, "text": text})
    return shown


def prescription_print_options(query, clinic):
    """Paper size and letterhead for the printed prescription, e.g. {"paper": "a4", "pad_space": 40}.

    Chosen on the print page itself (no data changes, so a plain GET):
      ?paper=a4  prints on A4 instead of A5.
      ?pad=40    is for pads that already have the clinic's letterhead printed on them: the
                 app's letterhead is left off the paper and 40 mm is kept blank at the top.
    Missing or unknown values fall back to the clinic's saved choice (Clinic settings:
    `Clinic.prescription_paper` / `Clinic.prescription_pad_space`), then to A5 with the
    letterhead printed.
    """
    allowed_spaces = {0, *PAD_SPACES}  # 0 = print the letterhead

    paper = query.get("paper", "")
    if paper not in PRESCRIPTION_PAPERS:
        paper = str(clinic.prescription_paper or "").lower()
    if paper not in PRESCRIPTION_PAPERS:
        paper = DEFAULT_PAPER

    pad = query.get("pad", "")
    if pad.isdecimal() and int(pad) in allowed_spaces:
        pad_space = int(pad)
    else:
        pad_space = clinic.prescription_pad_space
    if pad_space not in allowed_spaces:
        pad_space = 0
    return {"paper": paper, "pad_space": pad_space}


def medicine_suggestions(clinic, limit=MEDICINE_SUGGESTION_LIMIT):
    """Names of medicines this clinic has prescribed before (most used first, then sorted A–Z)."""
    rows = (
        PrescriptionItem.objects.filter(visit__clinic=clinic)
        .values("medicine")
        .annotate(times=Count("id"))
        .order_by("-times", "medicine")[:limit]
    )
    return sorted({row["medicine"] for row in rows}, key=str.lower)
