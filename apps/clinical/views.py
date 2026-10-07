"""Clinical pages: consultations (visits), printable prescriptions and lab results.

Rules followed on every page here (see docs/ARCHITECTURE.md):
  * Clinicians only (clinic owner or doctor). Receptionists get 403.
  * Every query is limited to request.clinic; another clinic's records give 404.
  * Viewing or changing patient data is written to the audit log (no clinical details in the summary).
"""

import logging
import re
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.appointments import scheduling
from apps.appointments import status as appointment_status
from apps.appointments.models import Appointment
from apps.core.audit import Action, log_action
from apps.core.models import AuditLog
from apps.core.permissions import CLINICAL_ROLES, ClinicianRequiredMixin, ClinicScopedMixin, clinic_required
from apps.patients.models import Patient
from apps.reminders import services as reminder_services

from .forms import (
    PRESCRIPTION_PREFIX,
    LabResultForm,
    VisitFilterForm,
    VisitForm,
    prescription_formset_class,
)
from .models import LabResult, Visit
from .uploads import content_type_for, safe_filename
from .utils import (
    PAD_SPACES,
    PRESCRIPTION_PAPERS,
    can_edit_visit,
    doctor_display_name,
    doctor_membership,
    medicine_suggestions,
    prescription_print_options,
    vitals_for_display,
)

logger = logging.getLogger(__name__)

# The side column of the visit form: how many earlier visits (the newest one in full) and lab results.
HISTORY_VISITS = 3
HISTORY_LABS = 3

# "Save" pressed twice on a new visit form (a slow connection, the browser re-sending the form) must
# not create the visit twice: there is no way to delete a visit. Each new-visit form carries a random
# token; the session remembers which visit each recent token created.
SAVED_VISITS_SESSION_KEY = "saved_visit_forms"
SAVED_VISITS_REMEMBERED = 20
FORM_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _id_param(request, name):
    """An integer id from the query string (?name=123), or None if it is missing or garbled."""
    value = request.GET.get(name, "")
    if value.isdecimal() and len(value) <= 18:
        return int(value)
    return None


def _patient_labs_url(patient):
    return reverse("patients:detail", args=[patient.pk]) + "?tab=labs"


# --- Visit list ----------------------------------------------------------------------

class VisitListView(ClinicianRequiredMixin, ListView):
    """All visits in the clinic, newest first, with simple filters."""

    template_name = "clinical/visit_list.html"
    context_object_name = "visits"
    paginate_by = 25

    def get_queryset(self):
        self.filter_form = VisitFilterForm(self.request.GET or None, clinic=self.request.clinic)
        visits = (
            Visit.objects.filter(clinic=self.request.clinic)
            .select_related("patient", "doctor")
            .order_by("-visit_date", "-pk")
        )
        return self.filter_form.filter(visits)

    def paginate_queryset(self, queryset, page_size):
        # get_page() falls back to a valid page for ?page=abc or ?page=999 instead of a 404.
        paginator = self.get_paginator(queryset, page_size)
        page = paginator.get_page(self.request.GET.get("page"))
        return paginator, page, page.object_list, page.has_other_pages()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        return context

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        # The list shows complaints and diagnoses: record whose (one row per page, never the search text).
        mrns = list(dict.fromkeys(visit.patient.mrn for visit in response.context_data["visits"]))
        if mrns:
            log_action(request, Action.VIEW, None, _visit_list_summary(mrns))
        return response


def _visit_list_summary(mrns):
    """'Viewed visit list: P-00012, P-00007': the patients on the page, as many as fit in an audit summary."""
    limit = AuditLog._meta.get_field("summary").max_length
    for shown in range(len(mrns), 0, -1):
        rest = len(mrns) - shown
        summary = "Viewed visit list: " + ", ".join(mrns[:shown]) + (f" (+{rest} more)" if rest else "")
        if len(summary) <= limit:
            return summary
    return f"Viewed visit list ({len(mrns)} patients)"


# --- Visit detail ------------------------------------------------------------------

class VisitDetailView(ClinicianRequiredMixin, ClinicScopedMixin, DetailView):
    model = Visit
    template_name = "clinical/visit_detail.html"
    context_object_name = "visit"

    def get_queryset(self):
        return super().get_queryset().select_related("patient", "doctor", "appointment", "created_by")

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        log_action(request, Action.VIEW, self.object, f"Viewed visit for {self.object.patient.mrn}")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visit = self.object
        context.update(
            patient=visit.patient,
            doctor_name=doctor_display_name(self.request.clinic, visit.doctor),
            vitals=vitals_for_display(visit),
            items=visit.prescription_items.all(),
            lab_results=visit.lab_results.filter(clinic=self.request.clinic).select_related("uploaded_by"),
            can_edit=can_edit_visit(self.request, visit),
        )
        return context


# --- New visit / edit visit ----------------------------------------------------------

@clinic_required(roles=CLINICAL_ROLES)
def visit_create(request, patient_pk):
    patient = get_object_or_404(Patient, pk=patient_pk, clinic=request.clinic)
    visit = Visit(clinic=request.clinic, patient=patient, doctor=request.user)
    if "appointment" in request.GET:
        # Started from an appointment (dashboard, schedule). An unusable id is ignored, not swapped for a guess.
        visit.appointment = _appointment_from_query(request, patient)
    else:
        # Started from the patient's page or a bookmark: still close the appointment they are waiting on.
        visit.appointment = _waiting_appointment_today(request, patient)
    return _visit_form_page(request, visit)


@clinic_required(roles=CLINICAL_ROLES)
def visit_update(request, pk):
    visit = get_object_or_404(
        Visit.objects.select_related("patient", "doctor", "appointment"), pk=pk, clinic=request.clinic
    )
    if not can_edit_visit(request, visit):
        raise PermissionDenied("Only the doctor who saw the patient or the clinic owner can change this visit.")
    return _visit_form_page(request, visit)


def _appointment_from_query(request, patient):
    """The appointment in ?appointment=<id>, only if it belongs to this clinic AND this patient.

    It must also be one that can be marked "Seen" (the same rule as the appointment
    status buttons): a cancelled or "did not come" appointment is ignored.
    """
    appointment_id = _id_param(request, "appointment")
    if appointment_id is None:
        return None
    appointment = Appointment.objects.filter(pk=appointment_id, clinic=request.clinic, patient=patient).first()
    if appointment is None:
        return None
    seen = Appointment.Status.COMPLETED
    if appointment.status != seen and not appointment_status.can_change(appointment.status, seen):
        return None
    return appointment


def _waiting_appointment_today(request, patient):
    """The patient's appointment in today's waiting room (status "Waiting"), or None.

    Prefers one booked with this doctor or with no doctor in particular; then the patient who
    arrived first, in the same order as the dashboard's waiting room. Only "Waiting" counts:
    a booked appointment, or one on another day, is never closed by an unrelated visit.
    """
    day_start, day_end = scheduling.day_range(timezone.localdate())  # the clinic's day (set by the middleware)
    waiting = Appointment.objects.filter(
        clinic=request.clinic,
        patient=patient,
        status=Appointment.Status.ARRIVED,
        scheduled_at__gte=day_start,
        scheduled_at__lt=day_end,
    ).order_by("updated_at", "pk")
    with_this_doctor = waiting.filter(Q(doctor=request.user) | Q(doctor__isnull=True))
    return with_this_doctor.first() or waiting.first()


def _visit_to_copy(request, patient):
    """The earlier visit in ?copy_from=<id> (same clinic and patient) whose medicines should be repeated."""
    visit_id = _id_param(request, "copy_from")
    if visit_id is None:
        return None
    return Visit.objects.filter(pk=visit_id, clinic=request.clinic, patient=patient).first()


def _rows_from(visit):
    """Prescription lines of `visit` as initial data for new rows."""
    fields = ["medicine", "dose", "frequency", "duration", "instructions"]
    return list(visit.prescription_items.values(*fields))


def _earlier_visits(visit, limit=HISTORY_VISITS):
    """The patient's latest visits before this one, newest first (for the side column).

    For a new visit that is simply the latest visits, even when the new one is put on an
    earlier day: the doctor wants to see where the patient is now.
    """
    visits = Visit.objects.filter(clinic=visit.clinic, patient=visit.patient)
    if visit.pk:
        visits = visits.filter(visit_date__lt=visit.visit_date).exclude(pk=visit.pk)
    visits = visits.select_related("doctor").prefetch_related("prescription_items").order_by("-visit_date", "-pk")
    return list(visits[:limit])


def _recent_labs(patient, limit=HISTORY_LABS):
    """The patient's latest lab results (for the side column)."""
    labs = LabResult.objects.filter(clinic=patient.clinic, patient=patient).order_by("-result_date", "-created_at")
    return list(labs[:limit])


def _last_prescription_visit(visit):
    """The patient's latest earlier visit that has medicines (for "Repeat last prescription")."""
    return (
        Visit.objects.filter(clinic=visit.clinic, patient=visit.patient, prescription_items__isnull=False)
        .distinct()
        .order_by("-visit_date")
        .first()
    )


def _visit_form_page(request, visit):
    """GET shows the visit form; POST validates and saves it. Shared by "new visit" and "edit visit"."""
    patient = visit.patient
    is_new = visit.pk is None
    copied_from = _visit_to_copy(request, patient) if is_new else None
    copied_rows = _rows_from(copied_from) if copied_from else []

    extra = max(3, len(copied_rows) + 1) if copied_rows else None
    PrescriptionRows = prescription_formset_class(editing=not is_new, extra=extra)

    form_token = _visit_form_token(request) if is_new else ""
    if request.method == "POST":
        already_saved = _visit_saved_with(request, form_token) if is_new else None
        if already_saved is not None:
            messages.info(request, "This visit was already saved.")
            return redirect(already_saved)
        form = VisitForm(request.POST, instance=visit)
        formset = PrescriptionRows(request.POST, instance=visit, prefix=PRESCRIPTION_PREFIX)
        form_ok = form.is_valid()
        formset_ok = formset.is_valid()
        if form_ok and formset_ok:
            visit = _save_visit(request, form, formset, is_new)
            if is_new:
                _remember_saved_visit(request, form_token, visit)
            messages.success(request, "Visit saved." if is_new else "Visit updated.")
            stale = formset.stale_medicines
            if stale:
                messages.warning(request, _stale_medicines_message(stale))
            return redirect(visit)
    else:
        form = VisitForm(instance=visit)
        formset = PrescriptionRows(instance=visit, prefix=PRESCRIPTION_PREFIX, initial=copied_rows)

    repeat_url = None
    last_rx_visit = _last_prescription_visit(visit) if is_new else None
    if last_rx_visit and last_rx_visit != copied_from:
        params = {"copy_from": last_rx_visit.pk}
        if visit.appointment_id:
            params["appointment"] = visit.appointment_id
        repeat_url = f"{request.path}?{urlencode(params)}"

    earlier_visits = _earlier_visits(visit)
    context = {
        "patient": patient,
        "visit": visit,
        "is_new": is_new,
        "form": form,
        "formset": formset,
        "doctor_name": doctor_display_name(request.clinic, visit.doctor),
        "appointment": visit.appointment if is_new else None,
        "copied_from": copied_from if copied_rows else None,
        "repeat_url": repeat_url,
        "last_rx_visit": last_rx_visit,
        "previous_visit": earlier_visits[0] if earlier_visits else None,
        "older_visits": earlier_visits[1:],
        "recent_labs": _recent_labs(patient),
        "medicine_names": medicine_suggestions(request.clinic),
        "cancel_url": patient.get_absolute_url() if is_new else visit.get_absolute_url(),
        "form_token": form_token,
    }
    # The page shows clinical data (earlier visits, labs, allergies, this visit's notes): audit it.
    # A successful save returned above and was audited as a create / update instead.
    if is_new:
        log_action(request, Action.VIEW, patient, f"Opened new visit form for {patient.mrn}")
    else:
        log_action(request, Action.VIEW, visit, f"Opened visit for editing for {patient.mrn}")
    return render(request, "clinical/visit_form.html", context)


def _visit_form_token(request):
    """The new-visit form's one-time token: the one it was sent with, or a fresh one for a new form."""
    token = request.POST.get("form_token", "") if request.method == "POST" else ""
    return token if FORM_TOKEN_RE.match(token) else uuid.uuid4().hex


def _visit_saved_with(request, token):
    """The visit this form already created (Save pressed again), or None."""
    visit_id = request.session.get(SAVED_VISITS_SESSION_KEY, {}).get(token)
    if visit_id is None:
        return None
    return Visit.objects.filter(pk=visit_id, clinic=request.clinic).first()


def _remember_saved_visit(request, token, visit):
    saved = request.session.get(SAVED_VISITS_SESSION_KEY, {})
    saved[token] = visit.pk
    # Only the most recent forms matter: a second press comes seconds after the first.
    request.session[SAVED_VISITS_SESSION_KEY] = dict(list(saved.items())[-SAVED_VISITS_REMEMBERED:])


def _stale_medicines_message(count):
    """Warning after saving when some medicine rows had already been removed (see PrescriptionFormSet)."""
    if count == 1:
        what = "One medicine had already been removed from this visit and was"
    else:
        what = f"{count} medicines had already been removed from this visit and were"
    return f"{what} not added back (the visit was changed in another tab or window). Please check the prescription."


def _save_visit(request, form, formset, is_new):
    """Save the visit and its medicines together, mark a linked appointment as seen, and audit it."""
    completed_appointment = None
    with transaction.atomic():
        visit = form.save(commit=False)
        if is_new:
            visit.created_by = request.user
            if form.is_back_dated:
                # An earlier visit typed in from a paper file is not today's consultation:
                # it must not close the appointment the patient may be waiting on right now.
                visit.appointment = None
        visit.save()
        formset.save_items(visit)

        mrn = visit.patient.mrn
        if is_new:
            log_action(request, Action.CREATE, visit, f"Recorded visit for {mrn}")
        else:
            log_action(request, Action.UPDATE, visit, f"Updated visit for {mrn}")

        # The patient has been seen: close the appointment the visit was started from.
        appointment = visit.appointment if is_new else None
        if appointment is not None and appointment.status != Appointment.Status.COMPLETED:
            appointment.status = Appointment.Status.COMPLETED
            appointment.save(update_fields=["status", "updated_at"])
            log_action(request, Action.UPDATE, appointment, f"Marked appointment as seen for {mrn}")
            completed_appointment = appointment

    _refresh_reminders(visit, completed_appointment)
    return visit


def _refresh_reminders(visit, completed_appointment=None):
    """Keep WhatsApp reminders in step with the visit.

    Runs after the visit is safely saved: a problem with reminders must never lose the
    doctor's notes (and the daily reminder job tidies up anyway), so errors are logged, not raised.
    """
    try:
        if completed_appointment is not None:
            reminder_services.refresh_for_appointment(completed_appointment)
        reminder_services.refresh_for_visit(visit)
    except Exception:
        logger.exception("Could not refresh reminders for visit %s", visit.pk)


# --- Printable prescription --------------------------------------------------------

@clinic_required(roles=CLINICAL_ROLES)
def prescription_print(request, pk):
    visit = get_object_or_404(
        Visit.objects.select_related("patient", "doctor", "clinic"), pk=pk, clinic=request.clinic
    )
    log_action(request, Action.VIEW, visit, f"Printed prescription for {visit.patient.mrn}")
    context = {
        "visit": visit,
        "patient": visit.patient,
        "clinic": visit.clinic,
        "membership": doctor_membership(visit.clinic, visit.doctor),
        "vitals": vitals_for_display(visit),
        "items": visit.prescription_items.all(),
        "options": prescription_print_options(request.GET, visit.clinic),
        "paper_choices": PRESCRIPTION_PAPERS.items(),
        "pad_choices": PAD_SPACES.items(),
    }
    return render(request, "clinical/prescription_print.html", context)


# --- Lab results ---------------------------------------------------------------------

@clinic_required(roles=CLINICAL_ROLES)
def lab_create(request, patient_pk):
    patient = get_object_or_404(Patient, pk=patient_pk, clinic=request.clinic)

    from_visit = None
    visit_id = _id_param(request, "visit")
    if visit_id is not None:
        from_visit = Visit.objects.filter(pk=visit_id, clinic=request.clinic, patient=patient).first()

    if request.method == "POST":
        form = LabResultForm(request.POST, request.FILES, clinic=request.clinic, patient=patient)
        if form.is_valid():
            with transaction.atomic():
                lab = form.save(commit=False)
                lab.clinic = request.clinic
                lab.patient = patient
                lab.uploaded_by = request.user
                upload = form.cleaned_data.get("file")
                if upload:
                    lab.original_filename = safe_filename(upload.name)
                lab.save()
                log_action(request, Action.CREATE, lab, f"Added lab result for {patient.mrn}")
            messages.success(request, "Lab result saved.")
            if from_visit is not None:
                return redirect(lab.visit or from_visit)
            return redirect(_patient_labs_url(patient))
    else:
        form = LabResultForm(clinic=request.clinic, patient=patient, initial={"visit": from_visit})

    context = {
        "patient": patient,
        "form": form,
        "from_visit": from_visit,
        "cancel_url": from_visit.get_absolute_url() if from_visit else _patient_labs_url(patient),
    }
    return render(request, "clinical/lab_form.html", context)


@clinic_required(roles=CLINICAL_ROLES)
def lab_update(request, pk):
    """Correct a lab result: a typo in the values, the date, the visit, or a clearer copy of the report.

    Choosing a new file replaces the old one, which is deleted once the change is saved.
    """
    lab = get_object_or_404(LabResult.objects.select_related("patient", "visit"), pk=pk, clinic=request.clinic)
    patient = lab.patient
    back_url = lab.visit.get_absolute_url() if lab.visit else _patient_labs_url(patient)
    old_file = lab.file.name if lab.file else ""
    storage = lab.file.storage

    if request.method == "POST":
        form = LabResultForm(request.POST, request.FILES, instance=lab, clinic=request.clinic, patient=patient)
        if form.is_valid():
            new_upload = request.FILES.get("file")
            with transaction.atomic():
                lab = form.save(commit=False)
                if new_upload:
                    lab.original_filename = safe_filename(new_upload.name)
                lab.save()
                log_action(request, Action.UPDATE, lab, f"Updated lab result for {patient.mrn}")
                if new_upload and old_file and lab.file.name != old_file:
                    # Remove the replaced file only once the new one is really saved.
                    transaction.on_commit(lambda: _delete_stored_file(storage, old_file, request.clinic.pk))
            messages.success(request, "Lab result saved.")
            return redirect(lab.visit or _patient_labs_url(patient))
    else:
        form = LabResultForm(instance=lab, clinic=request.clinic, patient=patient)

    log_action(request, Action.VIEW, lab, f"Opened lab result for editing for {patient.mrn}")
    context = {
        "patient": patient,
        "form": form,
        "lab": lab,
        "is_edit": True,
        "from_visit": lab.visit,
        "cancel_url": back_url,
    }
    return render(request, "clinical/lab_form.html", context)


@clinic_required(roles=CLINICAL_ROLES)
def lab_file(request, pk):
    """Stream a lab report to a clinician. Files are never reachable through a public URL."""
    lab = get_object_or_404(LabResult.objects.select_related("patient"), pk=pk, clinic=request.clinic)
    if not lab.file:
        raise Http404("This lab result has no file.")
    try:
        handle = lab.file.storage.open(lab.file.name, "rb")
    except OSError:  # includes FileNotFoundError
        logger.warning("Lab file missing from storage for lab result %s", lab.pk)
        raise Http404("The file could not be found.")

    log_action(request, Action.VIEW, lab, f"Opened lab report for {lab.patient.mrn}")
    response = FileResponse(
        handle,
        as_attachment=False,
        filename=lab.original_filename or safe_filename(lab.file.name),
        content_type=content_type_for(lab.file.name),
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@clinic_required(roles=CLINICAL_ROLES)
@require_POST
def lab_delete(request, pk):
    lab = get_object_or_404(LabResult.objects.select_related("patient"), pk=pk, clinic=request.clinic)
    patient = lab.patient
    stored_name = lab.file.name if lab.file else ""
    storage = lab.file.storage

    with transaction.atomic():
        log_action(request, Action.DELETE, lab, f"Deleted lab result for {patient.mrn}")
        lab.delete()
        if stored_name:
            # Remove the file only once the row is really gone (never a row pointing at a missing file).
            transaction.on_commit(lambda: _delete_stored_file(storage, stored_name, request.clinic.pk))

    messages.success(request, "Lab result deleted.")
    return redirect(_patient_labs_url(patient))


def _delete_stored_file(storage, name, clinic_id):
    try:
        storage.delete(name)
    except OSError:
        logger.warning("Could not delete stored file for a deleted lab result in clinic %s", clinic_id)
