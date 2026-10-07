"""Staff pages for appointments: day and week schedule, booking, editing and status changes.

Every role (owner, doctor, receptionist) may use these pages. Every query is limited to
`request.clinic`; another clinic's appointment or patient gives a 404.
"""

from collections import Counter, defaultdict
from datetime import timedelta

from django.contrib import messages
from django.db.models import Prefetch
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format, time_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership
from apps.core.audit import Action, log_action
from apps.core.permissions import clinic_required
from apps.patients.models import Patient
from apps.patients.services import search_patients
from apps.reminders import services as reminder_services
from apps.reminders.models import Reminder

from . import scheduling
from . import status as appt_status
from .forms import AppointmentForm, AppointmentUpdateForm
from .models import Appointment

Status = Appointment.Status

PICKER_RESULTS = 25


# --- small helpers ------------------------------------------------------------


def day_url(day, doctor=None):
    """Link to the day view of `day` (optionally filtered by doctor)."""
    url = f"{reverse('appointments:day')}?date={day.isoformat()}"
    if doctor is not None:
        url += f"&doctor={doctor.pk}"
    return url


def _safe_next(request, fallback=None):
    """The ?next= / POST next URL if it points to this site, otherwise `fallback`."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return fallback


def _int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _pick_doctor(raw, doctors):
    """The doctor chosen by ?doctor=<id>, if it is one of this clinic's doctors. Anything else is ignored."""
    doctor_id = _int_or_none(raw)
    return next((d for d in doctors if d.pk == doctor_id), None)


def _doctor_options(doctors, names):
    return [{"id": d.pk, "name": names.get(d.pk, d.full_name)} for d in doctors]


def _when_text(dt):
    """'7 Oct 2026 at 10:30 a.m.' in the current (clinic) timezone."""
    local = timezone.localtime(dt)
    return f"{date_format(local, 'j M Y')} at {time_format(local, 'g:i a')}"


def _default_doctor(request, doctors):
    """The doctor a new booking starts with when the link does not choose one.

    A clinic with one doctor: that doctor. A doctor booking in a bigger clinic: themselves.
    Anyone else (an owner may be the manager, not a doctor seeing patients): "Any doctor".
    """
    if len(doctors) == 1:
        return doctors[0]
    if request.membership.role == Membership.Role.DOCTOR:
        return next((d for d in doctors if d.pk == request.user.pk), None)
    return None


# --- day & week schedule ------------------------------------------------------


@clinic_required
def day_view(request):
    clinic = request.clinic
    today = timezone.localdate()
    day = scheduling.parse_date(request.GET.get("date"), default=today)
    doctors = list(clinic.doctors)
    names = scheduling.doctor_names(clinic)
    doctor = _pick_doctor(request.GET.get("doctor"), doctors)

    start, end = scheduling.day_range(day)
    appointments = list(
        Appointment.objects.filter(clinic=clinic, scheduled_at__gte=start, scheduled_at__lt=end)
        .select_related("patient", "doctor")
        .prefetch_related(
            Prefetch(
                "reminders",
                queryset=Reminder.objects.filter(kind=Reminder.Kind.APPOINTMENT),
                to_attr="appointment_reminders",
            )
        )
        .order_by("scheduled_at", "pk")
    )
    # Waiting room: first come, first served. Tokens are numbered across the whole clinic
    # (before the doctor filter), so they match the dashboard.
    waiting = scheduling.waiting_room(appointments)
    if doctor is not None:
        appointments = [a for a in appointments if a.doctor_id == doctor.pk]
        waiting = [a for a in waiting if a.doctor_id == doctor.pk]

    for appointment in appointments:
        appointment.doctor_name = (
            names.get(appointment.doctor_id, appointment.doctor.full_name) if appointment.doctor else ""
        )
        appointment.reminder = appointment.appointment_reminders[0] if appointment.appointment_reminders else None
        # Only booked / confirmed appointments of patients who agreed to reminders get one;
        # for the others "Not yet" would mislead.
        remindable = appointment.status in reminder_services.REMINDABLE_STATUSES
        appointment.reminders_off = remindable and not reminder_services.wants_reminders(appointment.patient)
        appointment.reminder_expected = remindable and not appointment.reminders_off

    counts = Counter(a.status for a in appointments)
    status_chips = [
        {"status": value, "label": label, "count": counts.get(value, 0)}
        for value, label in appt_status.STATUS_LABELS.items()
    ]
    # Schedule: by time, cancelled ones last.
    schedule = [a for a in appointments if a.status != Status.CANCELLED] + [
        a for a in appointments if a.status == Status.CANCELLED
    ]

    book_url = f"{reverse('appointments:create')}?date={day.isoformat()}"
    if doctor is not None:
        book_url += f"&doctor={doctor.pk}"

    context = {
        "day": day,
        "today": today,
        "is_today": day == today,
        "relative_day": {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((day - today).days, ""),
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1),
        "selected_doctor": doctor,
        "selected_doctor_name": names.get(doctor.pk, doctor.full_name) if doctor else "",
        "doctor_options": _doctor_options(doctors, names),
        "status_chips": status_chips,
        "active_count": len(appointments) - counts.get(Status.CANCELLED, 0),
        "waiting": waiting,
        "show_waiting_room": day == today or bool(waiting),
        "schedule": schedule,
        "book_url": book_url,
    }
    return render(request, "appointments/day.html", context)


@clinic_required
def week_view(request):
    clinic = request.clinic
    today = timezone.localdate()
    week_start = scheduling.monday_of(scheduling.parse_date(request.GET.get("start"), default=today))
    doctors = list(clinic.doctors)
    names = scheduling.doctor_names(clinic)
    doctor = _pick_doctor(request.GET.get("doctor"), doctors)

    start, end = scheduling.day_range(week_start, days=7)
    appointments = (
        Appointment.objects.filter(clinic=clinic, scheduled_at__gte=start, scheduled_at__lt=end)
        .select_related("patient")
        .order_by("scheduled_at", "pk")
    )
    if doctor is not None:
        appointments = appointments.filter(doctor=doctor)

    by_day = defaultdict(list)
    for appointment in appointments:
        by_day[scheduling.local_date(appointment.scheduled_at)].append(appointment)

    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        items = by_day.get(day, [])
        active = [a for a in items if a.status != Status.CANCELLED]
        cancelled = [a for a in items if a.status == Status.CANCELLED]
        days.append({
            "date": day,
            "is_today": day == today,
            "appointments": active + cancelled,
            "count": len(active),
            "url": day_url(day, doctor),
        })

    context = {
        "days": days,
        "week_start": week_start,
        "week_end": week_start + timedelta(days=6),
        "prev_week": week_start - timedelta(days=7),
        "next_week": week_start + timedelta(days=7),
        "is_this_week": week_start == scheduling.monday_of(today),
        "total": sum(d["count"] for d in days),
        "status_legend": list(appt_status.STATUS_LABELS.items()),
        "selected_doctor": doctor,
        "selected_doctor_name": names.get(doctor.pk, doctor.full_name) if doctor else "",
        "doctor_options": _doctor_options(doctors, names),
    }
    return render(request, "appointments/week.html", context)


# --- booking ------------------------------------------------------------------


def _patient_picker(request):
    """Step 1 of booking: find the patient. Works as a plain GET form (no JavaScript needed).

    Uses the same matching as the patient list (name, MR number, phone typed any way).
    """
    query = request.GET.get("q", "").strip()[:100]
    patients = Patient.objects.filter(clinic=request.clinic, is_archived=False)
    if query:
        patients = search_patients(patients, query, request.clinic.country).order_by("full_name")[:PICKER_RESULTS]
    else:
        patients = patients.order_by("-updated_at")[:10]
    context = {
        "query": query,
        "patients": patients,
        "day": scheduling.parse_date(request.GET.get("date")),
    }
    return render(request, "appointments/patient_picker.html", context)


@clinic_required
def create_view(request):
    raw_patient = request.GET.get("patient")
    if not raw_patient:
        return _patient_picker(request)

    clinic = request.clinic
    patient_id = _int_or_none(raw_patient)
    if patient_id is None:
        raise Http404("No such patient.")
    patient = get_object_or_404(Patient, pk=patient_id, clinic=clinic, is_archived=False)

    initial = {
        "date": scheduling.parse_date(request.GET.get("date"), default=timezone.localdate()),
        "duration_minutes": clinic.default_appointment_minutes,
    }
    doctors = list(clinic.doctors)
    doctor = _pick_doctor(request.GET.get("doctor"), doctors) or _default_doctor(request, doctors)
    if doctor is not None:
        initial["doctor"] = doctor.pk

    form = AppointmentForm(request.POST if request.method == "POST" else None, clinic=clinic, initial=initial)
    if form.is_valid():
        if form.cleaned_data.get("walk_in") and _waiting_today(clinic, patient):
            # "Patient is here now" sent twice (a double tap, the browser re-sending the form):
            # they are already in the waiting room, so don't give them a second token.
            messages.info(request, f"{patient.full_name} is already in the waiting room.")
            return redirect(day_url(timezone.localdate()))
        appointment = form.save(commit=False)
        appointment.clinic = clinic
        appointment.patient = patient
        appointment.created_by = request.user
        appointment.save()
        log_action(request, Action.CREATE, appointment, f"Booked appointment for {patient.mrn}")
        reminder_services.refresh_for_appointment(appointment)

        text = f"Appointment booked for {patient.full_name} on {_when_text(appointment.scheduled_at)}."
        if appointment.status == Status.ARRIVED:
            text += " They are in the waiting room."
        messages.success(request, text)
        return redirect(day_url(scheduling.local_date(appointment.scheduled_at)))

    context = {
        "form": form,
        "patient": patient,
        "is_create": True,
        "page_title": "Book appointment",
        "submit_label": "Book appointment",
        "cancel_url": day_url(initial["date"]),
    }
    return render(request, "appointments/form.html", context)


def _waiting_today(clinic, patient):
    """Is this patient already marked "Arrived" today (in the waiting room)?"""
    start, end = scheduling.day_range(timezone.localdate())
    return Appointment.objects.filter(
        clinic=clinic, patient=patient, status=Status.ARRIVED, scheduled_at__gte=start, scheduled_at__lt=end
    ).exists()


@clinic_required
def update_view(request, pk):
    clinic = request.clinic
    appointment = get_object_or_404(
        Appointment.objects.select_related("patient", "doctor", "created_by"), pk=pk, clinic=clinic
    )
    # Where the user came from (e.g. the day view with a doctor filter), if it is on this site.
    requested_next = _safe_next(request)
    back_url = requested_next or day_url(scheduling.local_date(appointment.scheduled_at))
    # Read before validation: an invalid POST still changes the in-memory instance.
    saved = {
        "status_label": appt_status.label(appointment.status),
        "status": appointment.status,
        "patient_note": appointment.patient_note,
        "patient_responded_at": appointment.patient_responded_at,
    }

    form = AppointmentUpdateForm(
        request.POST if request.method == "POST" else None, instance=appointment, clinic=clinic
    )
    if form.is_valid():
        rescheduled = form.rescheduled
        appointment = form.save()
        verb = "Rescheduled" if rescheduled else "Updated"
        log_action(request, Action.UPDATE, appointment, f"{verb} appointment for {appointment.patient.mrn}")
        reminder_services.refresh_for_appointment(appointment, rescheduled=rescheduled)

        if rescheduled:
            messages.success(request, f"Appointment moved to {_when_text(appointment.scheduled_at)}.")
        else:
            messages.success(request, "Appointment updated.")
        # Back where the user came from, otherwise to the (possibly new) day of the appointment.
        return redirect(requested_next or day_url(scheduling.local_date(appointment.scheduled_at)))

    context = {
        "form": form,
        "appointment": appointment,
        "patient": appointment.patient,
        "is_create": False,
        "page_title": "Edit appointment",
        "submit_label": "Save changes",
        "cancel_url": back_url,
        "next_url": requested_next,
        "saved": saved,
    }
    return render(request, "appointments/form.html", context)


# --- status changes -----------------------------------------------------------


@require_POST
@clinic_required
def set_status_view(request, pk):
    appointment = get_object_or_404(Appointment.objects.select_related("patient"), pk=pk, clinic=request.clinic)
    new_status = request.POST.get("status", "")
    if new_status not in Status.values:
        return HttpResponseBadRequest("Unknown appointment status.")

    next_url = _safe_next(request, day_url(scheduling.local_date(appointment.scheduled_at)))
    patient = appointment.patient
    old_label, new_label = appt_status.label(appointment.status), appt_status.label(new_status)

    if new_status == appointment.status:
        messages.info(request, f"{patient.full_name} is already marked as “{new_label}”.")
        return redirect(next_url)
    if not appt_status.can_change(appointment.status, new_status):
        messages.error(
            request,
            f"{patient.full_name}'s appointment is “{old_label}”, so it can't be changed to “{new_label}”.",
        )
        return redirect(next_url)

    appointment.status = new_status
    appointment.save(update_fields=["status", "updated_at"])
    log_action(request, Action.UPDATE, appointment, f"Marked {patient.mrn} as {new_label}")
    reminder_services.refresh_for_appointment(appointment)

    messages.success(request, f"{patient.full_name}: {new_label}.")
    return redirect(next_url)
