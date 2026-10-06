"""Patients: list & search, add / edit, the profile (medical-history dashboard), archive, CSV import.

Access rules (docs/ARCHITECTURE.md):
  * Everything is limited to `request.clinic`; another clinic's patient is a 404.
  * Receptionists manage contact details, appointments and reminders. Clinical data
    (visits, vitals, diagnoses, prescriptions, labs, allergies, chronic conditions, blood
    group) is never loaded for them, so it can't leak into the page.
  * Archive is for doctors / owners; CSV import is for the owner.
"""

from django.contrib import messages
from django.db import IntegrityError
from django.db.models import F, Max, Min, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView

from apps.accounts.models import Membership
from apps.appointments.models import Appointment
from apps.core.audit import Action, log_action
from apps.core.phone import normalize_phone
from apps.core.permissions import (
    CLINICAL_ROLES,
    OWNER_ONLY,
    ClinicRequiredMixin,
    clinic_required,
    is_clinician,
)
from apps.reminders.models import Reminder

from . import importer
from .forms import PatientForm, PatientImportForm
from .models import Patient
from .services import age_sex_label, clinical_summary, history_timeline, search_patients

SEARCH_JSON_LIMIT = 8
LIST_PAGE_SIZE = 25
TAB_LIMIT = 100
IMPORT_ERRORS_SHOWN = 200


# --- List & live search -----------------------------------------------------------


class PatientListView(ClinicRequiredMixin, ListView):
    template_name = "patients/patient_list.html"
    context_object_name = "patients"
    paginate_by = LIST_PAGE_SIZE

    SORTS = {
        "name": ("Name (A–Z)", ["full_name", "pk"]),
        "recent": ("Recently added", ["-created_at", "-pk"]),
        "last_visit": ("Last visit", [F("last_visit").desc(nulls_last=True), "full_name", "pk"]),
    }

    def get_sort(self):
        sort = self.request.GET.get("sort", "name")
        return sort if sort in self.SORTS else "name"

    def get_queryset(self):
        clinic = self.request.clinic
        show_archived = self.request.GET.get("archived") == "1"
        patients = Patient.objects.filter(clinic=clinic, is_archived=show_archived)
        patients = search_patients(patients, self.request.GET.get("q", ""), clinic.country)
        patients = patients.annotate(
            last_visit=Max("visits__visit_date"),
            next_appointment=Min(
                "appointments__scheduled_at",
                filter=Q(
                    appointments__scheduled_at__gte=timezone.now(),
                    appointments__status__in=Appointment.ACTIVE_STATUSES,
                ),
            ),
        )
        return patients.order_by(*self.SORTS[self.get_sort()][1])

    def paginate_queryset(self, queryset, page_size):
        """Like ListView's, but a bad or too-high ?page= shows the nearest page instead of a 404."""
        paginator = self.get_paginator(queryset, page_size)
        page = paginator.get_page(self.request.GET.get("page"))
        return paginator, page, page.object_list, page.has_other_pages()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "q": self.request.GET.get("q", "").strip(),
            "sort": self.get_sort(),
            "sort_choices": [(key, label) for key, (label, _) in self.SORTS.items()],
            "show_archived": self.request.GET.get("archived") == "1",
        })
        return context


@require_GET
@clinic_required
def search_json(request):
    """Live search for the top bar: at most 8 active patients, no clinical data."""
    query = request.GET.get("q", "").strip()
    results = []
    if len(query) >= 2:
        patients = Patient.objects.filter(clinic=request.clinic, is_archived=False)
        patients = search_patients(patients, query, request.clinic.country).order_by("full_name", "pk")
        results = [
            {
                "id": patient.pk,
                "name": patient.full_name,
                "mrn": patient.mrn,
                "phone": patient.phone,
                "age_sex": age_sex_label(patient),
                "url": patient.get_absolute_url(),
            }
            for patient in patients[:SEARCH_JSON_LIMIT]
        ]
    return JsonResponse({"results": results})


# --- Add / edit --------------------------------------------------------------------


class PatientFormMixin(ClinicRequiredMixin):
    """Shared by add and edit: builds PatientForm and decides where to go after saving."""

    template_name = "patients/patient_form.html"

    def get_patient(self):
        return Patient(clinic=self.request.clinic)

    def make_form(self, patient):
        return PatientForm(
            self.request.POST if self.request.method == "POST" else None,
            instance=patient,
            clinic=self.request.clinic,
            is_clinician=is_clinician(self.request),
        )

    def render_form(self, form, patient):
        return render(self.request, self.template_name, {"form": form, "patient": patient})

    def get(self, request, *args, **kwargs):
        patient = self.get_patient()
        return self.render_form(self.make_form(patient), patient)

    def post(self, request, *args, **kwargs):
        patient = self.get_patient()
        form = self.make_form(patient)
        if not form.is_valid():
            return self.render_form(form, patient)
        patient = self.save(form)
        if request.POST.get("next_action") == "book":
            return redirect(f"{reverse('appointments:create')}?patient={patient.pk}")
        return redirect(patient)


class PatientCreateView(PatientFormMixin, View):
    def save(self, form):
        patient = form.save(commit=False)
        patient.created_by = self.request.user
        patient.save()
        log_action(self.request, Action.CREATE, patient, f"Added patient {patient.mrn}")
        messages.success(self.request, f"{patient.full_name} added (MR number {patient.mrn}).")
        return patient


class PatientUpdateView(PatientFormMixin, View):
    def get_patient(self):
        return get_object_or_404(Patient, pk=self.kwargs["pk"], clinic=self.request.clinic)

    def save(self, form):
        patient = form.save()
        log_action(self.request, Action.UPDATE, patient, f"Updated patient {patient.mrn}")
        messages.success(self.request, "Patient details saved.")
        return patient


# --- Profile (medical-history dashboard) ---------------------------------------------


CLINICIAN_TABS = [("history", "History"), ("appointments", "Appointments"), ("labs", "Lab results"), ("messages", "Messages")]
RECEPTION_TABS = [("appointments", "Appointments"), ("messages", "Messages")]


class PatientDetailView(ClinicRequiredMixin, View):
    template_name = "patients/patient_detail.html"

    def get(self, request, pk):
        patient = get_object_or_404(Patient, pk=pk, clinic=request.clinic)
        clinician = is_clinician(request)
        tabs = CLINICIAN_TABS if clinician else RECEPTION_TABS
        tab = request.GET.get("tab")
        if tab not in dict(tabs):
            tab = tabs[0][0]  # history for clinicians, appointments for receptionists

        log_action(request, Action.VIEW, patient, f"Viewed patient {patient.mrn}")

        context = {
            "patient": patient,
            "tabs": tabs,
            "tab": tab,
            "phone_tel": normalize_phone(patient.phone, request.clinic.country),
            "reminders_off": not (patient.reminders_opt_in and patient.whatsapp_number),
            "upcoming_count": self.upcoming_appointments(patient).count(),
        }
        if clinician:
            context["summary"] = clinical_summary(patient)

        doctor_names = self.doctor_names(request.clinic)
        if tab == "history":
            entries, total = history_timeline(patient, doctor_names, limit=TAB_LIMIT)
            context.update(timeline=entries, timeline_total=total, timeline_limit=TAB_LIMIT)
        elif tab == "appointments":
            context.update(self.appointments_context(patient, doctor_names))
        elif tab == "labs":
            context["lab_results"] = patient.lab_results.select_related("visit").order_by(
                "-result_date", "-created_at"
            )[:TAB_LIMIT]
        elif tab == "messages":
            context["reminders"] = (
                Reminder.objects.filter(clinic=request.clinic, patient=patient)
                .select_related("sent_by")
                .order_by("-due_date", "-pk")[:TAB_LIMIT]
            )
        return render(request, self.template_name, context)

    @staticmethod
    def doctor_names(clinic):
        """user id -> 'Dr. Bilal Hussain' (titles live on the clinic membership)."""
        memberships = Membership.objects.filter(clinic=clinic).select_related("user")
        return {m.user_id: m.display_name for m in memberships}

    @staticmethod
    def upcoming_appointments(patient):
        start_of_today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        return patient.appointments.filter(
            status__in=Appointment.ACTIVE_STATUSES, scheduled_at__gte=start_of_today
        )

    def appointments_context(self, patient, doctor_names):
        upcoming = list(self.upcoming_appointments(patient).select_related("doctor").order_by("scheduled_at"))
        past = list(
            patient.appointments.exclude(pk__in=[a.pk for a in upcoming])
            .select_related("doctor")
            .order_by("-scheduled_at")[:TAB_LIMIT]
        )
        for appointment in upcoming + past:
            appointment.doctor_name = doctor_names.get(appointment.doctor_id, "")
        return {"upcoming_appointments": upcoming, "past_appointments": past, "tab_limit": TAB_LIMIT}


# --- Archive ---------------------------------------------------------------------------


@require_POST
@clinic_required(roles=CLINICAL_ROLES)
def archive(request, pk):
    """Archive or restore (toggle). Archived patients are hidden from the list and search."""
    patient = get_object_or_404(Patient, pk=pk, clinic=request.clinic)
    patient.is_archived = not patient.is_archived
    patient.save(update_fields=["is_archived", "updated_at"])
    if patient.is_archived:
        log_action(request, Action.UPDATE, patient, f"Archived patient {patient.mrn}")
        messages.success(request, f"{patient.full_name} archived. They no longer show in the patient list or search.")
    else:
        log_action(request, Action.UPDATE, patient, f"Restored patient {patient.mrn}")
        messages.success(request, f"{patient.full_name} restored.")
    return redirect(patient)


# --- CSV import (owner) -------------------------------------------------------------------


@clinic_required(roles=OWNER_ONLY)
def import_patients(request):
    if request.method == "POST":
        form = PatientImportForm(request.POST, request.FILES)
    else:
        form = PatientImportForm()
    context = {
        "form": form,
        "columns": [(name, required, help) for name, (required, help) in importer.COLUMNS.items()],
        "max_rows": importer.MAX_ROWS,
        "errors": [],
    }
    if request.method == "POST" and form.is_valid():
        try:
            result = importer.parse_patients_csv(form.cleaned_data["file"].read(), request.clinic, request.user)
        except importer.ImportFileError as problem:
            form.add_error("file", str(problem))
        else:
            if result.ok:
                try:
                    count = importer.save_imported_patients(request.clinic, result.patients)
                except IntegrityError:
                    # Someone saved a patient with one of these MR numbers while we were checking.
                    # The import ran in one transaction, so nothing was saved.
                    form.add_error(
                        "file", "Some MR numbers were taken by another patient just now. Nothing was imported: "
                        "please upload the file again."
                    )
                else:
                    log_action(request, Action.IMPORT, None, f"Imported {count} patients from CSV")
                    messages.success(request, f"Imported {count} patient{'' if count == 1 else 's'}.")
                    return redirect(f"{reverse('patients:list')}?sort=recent")
            else:
                context["errors"] = result.errors[:IMPORT_ERRORS_SHOWN]
                context["errors_total"] = len(result.errors)
                context["error_rows"] = len({row for row, _ in result.errors})
    return render(request, "patients/patient_import.html", context)


@require_GET
@clinic_required(roles=OWNER_ONLY)
def import_template(request):
    response = HttpResponse(importer.template_csv(request.clinic.country), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="patients-import-template.csv"'
    return response
