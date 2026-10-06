"""Patient helpers shared by the views, forms and CSV import.

Kept out of views.py so each piece can be read (and tested) on its own:
  * search_patients()          - the matching rules behind the list page and the live search box
  * find_possible_duplicates() - "is this patient already registered?" check on the new-patient form
  * phone_example()            - a country-appropriate example number for help texts and errors
  * vitals_summary(), clinical_summary(), history_timeline() - the profile page's medical history
"""

import re
from datetime import datetime, time

from django.db.models import Count, Q, Value
from django.db.models.functions import Replace
from django.utils import timezone

from apps.core.phone import normalize_phone

from .models import Patient

# --- Phone examples -----------------------------------------------------------

PHONE_EXAMPLES = {"PK": "0300-1234567", "IN": "98765 43210"}


def phone_example(country):
    """How staff in this clinic's country usually write a mobile number."""
    return PHONE_EXAMPLES.get(country, PHONE_EXAMPLES["PK"])


# --- Search -------------------------------------------------------------------

# A query made only of digits and the characters people type inside phone numbers.
PHONE_QUERY_RE = re.compile(r"^[\d\s\-+().]+$")
MIN_PHONE_DIGITS = 3


def _with_phone_digits(queryset):
    """Annotate `phone_digits`: the mobile number as typed, minus dashes, spaces and '+'."""
    digits = "phone"
    for separator in ("-", " ", "+", "(", ")", "."):
        digits = Replace(digits, Value(separator), Value(""))
    return queryset.annotate(phone_digits=digits)


def search_patients(queryset, query, country):
    """Filter `queryset` to patients matching `query`.

    Matches the name or MR number (anywhere, any case) and, for queries that look like
    a phone number, the mobile / WhatsApp number however it was typed:
    "0300 123", "03001234567", "+92 300 1234567" and "4567" all find "0300-1234567".
    """
    query = (query or "").strip()
    if not query:
        return queryset

    condition = Q(full_name__icontains=query) | Q(mrn__icontains=query) | Q(phone__icontains=query)

    if PHONE_QUERY_RE.match(query):
        digits = re.sub(r"\D", "", query)
        if len(digits) >= MIN_PHONE_DIGITS:
            queryset = _with_phone_digits(queryset)
            condition |= Q(phone_digits__contains=digits)
            # whatsapp_number is stored internationally (923001234567), so drop the
            # leading 0 of a local number: "0300 123" -> "300123".
            national = digits.lstrip("0")
            if len(national) >= MIN_PHONE_DIGITS:
                condition |= Q(whatsapp_number__contains=national)
            normalized = normalize_phone(query, country)
            if normalized:
                condition |= Q(whatsapp_number=normalized)

    return queryset.filter(condition)


def age_sex_label(patient):
    """'34 y · M', '8 mo · F', or just 'M' when the age is unknown."""
    parts = [patient.age_display, patient.sex]
    return " · ".join(part for part in parts if part)


# --- Duplicate check ------------------------------------------------------------


def find_possible_duplicates(clinic, *, full_name, date_of_birth, whatsapp_number, exclude_pk=None):
    """Active patients in this clinic who may be the same person.

    Same WhatsApp number, or same name (any case) and same date of birth.
    """
    condition = Q()
    if whatsapp_number:
        condition |= Q(whatsapp_number=whatsapp_number)
    if full_name and date_of_birth:
        condition |= Q(full_name__iexact=full_name.strip(), date_of_birth=date_of_birth)
    if not condition:
        return Patient.objects.none()

    matches = Patient.objects.filter(clinic=clinic, is_archived=False).filter(condition)
    if exclude_pk:
        matches = matches.exclude(pk=exclude_pk)
    return matches.order_by("full_name", "pk")


# --- Medical history (clinicians only) ---------------------------------------------

VITAL_FIELDS = (
    "bp_systolic", "bp_diastolic", "pulse", "temperature_c", "spo2", "weight_kg", "height_cm", "blood_sugar",
)


def vitals_summary(visit):
    """Short readings for one visit, e.g. ['BP 120/80', 'Pulse 72', 'Temp 37.0 °C']."""
    if visit is None:
        return []
    readings = []
    if visit.blood_pressure:
        readings.append(f"BP {visit.blood_pressure}")
    if visit.pulse:
        readings.append(f"Pulse {visit.pulse}")
    if visit.temperature_c is not None:
        readings.append(f"Temp {visit.temperature_c} °C")
    if visit.spo2:
        readings.append(f"SpO₂ {visit.spo2}%")
    if visit.weight_kg is not None:
        readings.append(f"Weight {visit.weight_kg} kg")
    if visit.bmi:
        readings.append(f"BMI {visit.bmi}")
    if visit.blood_sugar:
        readings.append(f"Sugar {visit.blood_sugar} mg/dL")
    return readings


def _has_any_vital():
    condition = Q()
    for name in VITAL_FIELDS:
        condition |= Q(**{f"{name}__isnull": False})
    return condition


def follow_up_status(patient, latest_visit, today=None):
    """What happened to the latest visit's follow-up date.

    Returns None when no follow-up was set, otherwise a dict:
      {"due": date, "state": "booked" | "overdue" | "due", "appointment": Appointment or None}
    """
    from apps.appointments.models import Appointment

    if latest_visit is None or latest_visit.follow_up_date is None:
        return None
    today = today or timezone.localdate()

    # `latest_visit` is the newest visit, so "came back" can only mean a later appointment
    # (not the one this visit was booked under, and not one that was cancelled or missed).
    later_appointments = patient.appointments.filter(scheduled_at__gt=latest_visit.visit_date).exclude(
        status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW]
    )
    if latest_visit.appointment_id:
        later_appointments = later_appointments.exclude(pk=latest_visit.appointment_id)
    came_back_or_booked = later_appointments.order_by("scheduled_at").first()
    if came_back_or_booked is not None:
        state = "booked"
    elif latest_visit.follow_up_date < today:
        state = "overdue"
    else:
        state = "due"
    return {"due": latest_visit.follow_up_date, "state": state, "appointment": came_back_or_booked}


def clinical_summary(patient):
    """Everything the 'Clinical summary' card needs. Call for clinicians only."""
    visits = patient.visits.all()
    latest_visit = visits.order_by("-visit_date", "-pk").first()
    vitals_visit = visits.filter(_has_any_vital()).order_by("-visit_date", "-pk").first()
    medicines_visit = (
        visits.filter(prescription_items__isnull=False).distinct().order_by("-visit_date", "-pk").first()
    )
    return {
        "latest_visit": latest_visit,
        "vitals_visit": vitals_visit,
        "vitals": vitals_summary(vitals_visit),
        "medicines_visit": medicines_visit,
        "medicines": list(medicines_visit.prescription_items.all()) if medicines_visit else [],
        "follow_up": follow_up_status(patient, latest_visit),
    }


HISTORY_LIMIT = 100


def _sort_key(value):
    """Timeline entries mix local datetimes (visits, appointments) and plain dates (labs)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.combine(value, time.min)


def history_timeline(patient, doctor_names, limit=HISTORY_LIMIT):
    """Visits, lab results and past appointments without a visit, newest first.

    Returns (entries, total). Each entry is a dict with `kind` ("visit" | "lab" | "appointment"),
    `when` (date or datetime for display) and `obj`; visits also carry `vitals` and `doctor_name`.
    """
    now = timezone.now()
    visits = (
        patient.visits.select_related("doctor")
        .annotate(medicine_count=Count("prescription_items"))
        .order_by("-visit_date", "-pk")
    )
    labs = patient.lab_results.order_by("-result_date", "-created_at")
    missed = (
        patient.appointments.filter(scheduled_at__lt=now, visits__isnull=True)
        .select_related("doctor")
        .order_by("-scheduled_at")
    )
    total = visits.count() + labs.count() + missed.count()

    entries = []
    for visit in visits[:limit]:
        local = timezone.localtime(visit.visit_date)
        entries.append({
            "kind": "visit", "when": local, "obj": visit,
            "sort": _sort_key(local),
            "vitals": vitals_summary(visit),
            "doctor_name": doctor_names.get(visit.doctor_id, str(visit.doctor)),
        })
    for lab in labs[:limit]:
        entries.append({"kind": "lab", "when": lab.result_date, "obj": lab, "sort": _sort_key(lab.result_date)})
    for appointment in missed[:limit]:
        local = timezone.localtime(appointment.scheduled_at)
        entries.append({
            "kind": "appointment", "when": local, "obj": appointment,
            "sort": _sort_key(local),
            "doctor_name": doctor_names.get(appointment.doctor_id, str(appointment.doctor or "")),
        })

    entries.sort(key=lambda entry: entry["sort"], reverse=True)
    return entries[:limit], total
