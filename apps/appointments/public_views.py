"""The patient's confirmation page, opened from the link in a WhatsApp reminder: /c/<token>/

No login: the long random token in the link is the only key. The page shows as little as
possible (clinic, patient's first name, date/time, doctor) and nothing clinical.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.accounts.models import Membership
from apps.core.audit import Action, log_action
from apps.core.phone import normalize_phone
from apps.reminders import services as reminder_services

from .forms import PatientResponseForm
from .models import Appointment
from .status import PATIENT_CAN_RESPOND

Status = Appointment.Status


def _clinic_zone(clinic):
    try:
        return ZoneInfo(clinic.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def _doctor_display_name(appointment):
    """'Dr. Bilal Hussain' using the doctor's title in this clinic, or '' for 'any doctor'."""
    if appointment.doctor is None:
        return ""
    membership = Membership.objects.filter(user=appointment.doctor, clinic=appointment.clinic).first()
    return membership.display_name if membership else appointment.doctor.full_name


def _can_respond(appointment):
    return appointment.status in PATIENT_CAN_RESPOND and appointment.scheduled_at > timezone.now()


def _record_answer(request, appointment, form):
    if form.cleaned_data["answer"] == PatientResponseForm.CONFIRM:
        appointment.status = Status.CONFIRMED
        appointment.patient_note = ""
        summary = "Patient confirmed via link"
    else:
        appointment.status = Status.RESCHEDULE_REQUESTED
        appointment.patient_note = form.cleaned_data["note"].strip()
        summary = "Patient asked for another time"
    appointment.patient_responded_at = timezone.now()
    appointment.save(update_fields=["status", "patient_note", "patient_responded_at", "updated_at"])
    log_action(request, Action.UPDATE, appointment, summary, clinic=appointment.clinic)
    reminder_services.refresh_for_appointment(appointment)


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def confirm_view(request, token):
    appointment = get_object_or_404(
        Appointment.objects.select_related("clinic", "patient", "doctor"),
        confirm_token=token,
        clinic__is_active=True,
    )
    can_respond = _can_respond(appointment)
    form = PatientResponseForm(request.POST if request.method == "POST" else None)
    status_code = 200

    if request.method == "POST":
        if not can_respond:
            status_code = 400  # past, cancelled, seen or did-not-come: show the "please call" page
        elif form.is_valid():
            _record_answer(request, appointment, form)
            return redirect("public:confirm", token=appointment.confirm_token)

    clinic = appointment.clinic
    context = {
        "clinic": clinic,
        "clinic_tel": normalize_phone(clinic.phone, clinic.country) if clinic.phone else "",
        "first_name": appointment.patient.first_name,
        "when": appointment.scheduled_at,
        "doctor_name": _doctor_display_name(appointment),
        "status": appointment.status,
        "responded": appointment.patient_responded_at is not None,
        "can_respond": can_respond,
        "form": form,
    }
    # Show the time in the clinic's timezone, whoever opens the link.
    with timezone.override(_clinic_zone(clinic)):
        return render(request, "appointments/public_confirm.html", context, status=status_code)
