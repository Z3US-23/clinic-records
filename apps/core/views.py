"""Core pages: the "Today" dashboard, audit log, export & backup, and the installable-app (PWA) files."""

import hashlib
import json
import logging
import re
from datetime import timedelta
from functools import lru_cache

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_date
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.accounts.models import Clinic, Membership, User
from apps.appointments.models import Appointment
from apps.appointments.scheduling import waiting_room
from apps.clinical.models import Visit
from apps.patients.models import Patient
from apps.reminders.models import MessageTemplate, Reminder
from apps.reminders.services import booked_appointments, generate_reminders, overdue_grace_days

from .audit import Action, log_action
from .exports import build_clinic_export, export_counts
from .models import AuditLog, SetupChecklist
from .permissions import OWNER_ONLY, clinic_required
from .services import MISSED_FOLLOW_UP_WINDOW_DAYS, day_bounds, missed_follow_ups, reschedule_requests

logger = logging.getLogger(__name__)

# How many "asking for another time" requests the Today page lists (its badge shows the total).
RESCHEDULE_LIST_LIMIT = 10


# --- Helpers --------------------------------------------------------------------------------

def greeting_for(moment):
    if moment.hour < 12:
        return "Good morning"
    if moment.hour < 17:
        return "Good afternoon"
    return "Good evening"


def greeting_name(membership):
    """'Dr. Sara' for a doctor with a title, otherwise just the first name ('Hina')."""
    return f"{membership.title} {membership.user.get_short_name()}".strip()


def staff_display_names(clinic):
    """{user_id: 'Dr. Bilal Hussain'} for everyone in the clinic: one query instead of one per row."""
    memberships = Membership.objects.filter(clinic=clinic).select_related("user")
    return {m.user_id: m.display_name for m in memberships}


def doctor_name(appointment, names):
    """The booked doctor's display name ('' for "any doctor"), looked up in staff_display_names()."""
    if not appointment.doctor_id:
        return ""
    return names.get(appointment.doctor_id) or appointment.doctor.full_name


def relative_day(day, today):
    """'Today', 'Tomorrow' or 'Wed 8 Oct'."""
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return format_date(day, "D j M")


def whatsapp_blocker(patient):
    """Why a reminder can't be sent to this patient on WhatsApp, or '' if it can."""
    if not patient.whatsapp_number:
        return "No WhatsApp number"
    if not patient.reminders_opt_in:
        return "Doesn't want reminders"
    if patient.is_archived:
        return "Patient archived"
    return ""


# --- Today (dashboard) ----------------------------------------------------------------------

@require_safe
@clinic_required
def dashboard(request):
    clinic = request.clinic
    membership = request.membership

    # Make sure today's reminders exist before we count them. Idempotent, so safe on every visit;
    # a problem there must not take down the home page.
    try:
        generate_reminders(clinic)
    except Exception:
        logger.exception("Could not prepare reminders for clinic %s", clinic.pk)

    now = timezone.localtime()
    today = now.date()
    day_start, day_end = day_bounds(today)
    names = staff_display_names(clinic)

    # Today's appointments (cancelled ones are left out). The waiting room is the "arrived" ones.
    appointments = list(
        Appointment.objects.filter(clinic=clinic, scheduled_at__gte=day_start, scheduled_at__lt=day_end)
        .exclude(status=Appointment.Status.CANCELLED)
        .select_related("patient", "doctor")
        .order_by("scheduled_at", "pk")
    )
    for appt in appointments:
        appt.doctor_name = doctor_name(appt, names)
    # In arrival order, with the same token numbers as the day view.
    waiting = waiting_room(appointments)

    # Patients who tapped "I need another time" on their confirmation link (mostly about tomorrow).
    requests = reschedule_requests(clinic, today)
    asking_new_time = list(requests[:RESCHEDULE_LIST_LIMIT])
    for appt in asking_new_time:
        appt.doctor_name = doctor_name(appt, names)
        appt.day_label = relative_day(timezone.localtime(appt.scheduled_at).date(), today)
    asking_total = len(asking_new_time) if len(asking_new_time) < RESCHEDULE_LIST_LIMIT else requests.count()

    # Reminders.
    reminders_due = Reminder.objects.filter(clinic=clinic, status=Reminder.Status.PENDING, due_date__lte=today)
    reminders_to_send = list(reminders_due.select_related("patient").order_by("due_date", "pk")[:5])
    for reminder in reminders_to_send:
        reminder.blocker = whatsapp_blocker(reminder.patient)

    # Patients.
    month_start, _ = day_bounds(today.replace(day=1))
    patient_stats = Patient.objects.filter(clinic=clinic).aggregate(
        all=Count("pk"),
        active=Count("pk", filter=Q(is_archived=False)),
        new_this_month=Count("pk", filter=Q(is_archived=False, created_at__gte=month_start)),
    )

    context = {
        "greeting": greeting_for(now),
        "greeting_name": greeting_name(membership),
        "today": today,
        "stats": {
            "appointments_today": len(appointments),
            "waiting": len(waiting),
            "reschedule_requests": asking_total,
            "reminders_due": reminders_due.count(),
            "missed_follow_ups": missed_follow_ups(clinic, today).count(),
            "patients": patient_stats["active"],
            "new_this_month": patient_stats["new_this_month"],
        },
        "waiting": waiting,
        "appointments": appointments,
        "asking_new_time": asking_new_time,
        "reminders_to_send": reminders_to_send,
        "coming_back": coming_back_soon(clinic, today, day_start),
        # Clinical text (diagnoses) only ever goes to doctors and owners.
        "recent_visits": recent_visits(clinic, request.user) if membership.is_clinician else [],
        "onboarding_steps": [],
    }
    if membership.is_owner:
        steps = setup_checklist(clinic, request.user, patient_count=patient_stats["all"])
        context["onboarding_steps"] = steps
        context["onboarding_done"] = sum(step["done"] for step in steps)
    return render(request, "core/dashboard.html", context)


def coming_back_soon(clinic, today, day_start, days=7, limit=10):
    """Patients whose follow-up date falls in the next week and who haven't been seen since.

    `is_booked` uses the reminders app's rule, so a patient who asked for another time (or whose
    old booking nobody closed) is not shown as booked.
    """
    seen_again = Visit.objects.filter(patient=OuterRef("patient"), visit_date__gt=OuterRef("visit_date"))
    booked = booked_appointments(OuterRef("patient_id"), OuterRef("visit_date"), OuterRef("appointment_id"), day_start)
    return list(
        Visit.objects.filter(
            clinic=clinic,
            follow_up_date__gte=today,
            follow_up_date__lte=today + timedelta(days=days),
            patient__is_archived=False,
        )
        .filter(~Exists(seen_again))
        .annotate(is_booked=Exists(booked))
        .select_related("patient")
        .order_by("follow_up_date", "patient__full_name")[:limit]
    )


def recent_visits(clinic, doctor, limit=5):
    return list(
        Visit.objects.filter(clinic=clinic, doctor=doctor).select_related("patient").order_by("-visit_date")[:limit]
    )


# --- "Get your clinic ready" checklist (owners) ---------------------------------------------

# The reminders:templates page writes an audit entry each time the owner saves or resets a message
# ("Updated appointment message template"). Saving the standard wording stores no MessageTemplate
# (on purpose, so later improvements reach the clinic), so that entry is what shows it was reviewed.
TEMPLATE_SAVED_AUDIT_TEXT = "message template"


def setup_checklist(clinic, owner, patient_count):
    """The owner's checklist steps, or [] once every step is done or the owner has hidden it.

    One query, however many steps there are.
    """
    flags = (
        Clinic.objects.filter(pk=clinic.pk)
        .annotate(
            hidden=Exists(SetupChecklist.objects.filter(clinic=OuterRef("pk"), hidden_at__isnull=False)),
            has_staff=Exists(
                Membership.objects.filter(clinic=OuterRef("pk"), accepted_at__isnull=False).exclude(user=owner)
            ),
            has_own_wording=Exists(MessageTemplate.objects.filter(clinic=OuterRef("pk"))),
            saved_wording=Exists(
                AuditLog.objects.filter(
                    clinic=OuterRef("pk"), action=Action.UPDATE, summary__contains=TEMPLATE_SAVED_AUDIT_TEXT
                )
            ),
        )
        .values("hidden", "has_staff", "has_own_wording", "saved_wording")
        .get()
    )
    if flags["hidden"]:
        return []
    steps = [
        {
            "label": "Add your patients",
            "text": "One at a time (name, phone number and age are enough), or all at once from Excel.",
            "url": reverse("patients:create"),
            "extra_url": reverse("patients:import"),
            "extra_label": "Import a spreadsheet",
            "done": patient_count > 0,
        },
        {
            "label": "Add staff",
            "text": "Give each doctor and receptionist their own login.",
            "url": reverse("accounts:staff_list"),
            "done": flags["has_staff"],
        },
        {
            "label": "Set clinic details for prescriptions",
            "text": "Address, phone number and timings printed at the top of every prescription.",
            "url": reverse("accounts:clinic_settings"),
            "done": bool(clinic.phone and (clinic.address or clinic.prescription_header)),
        },
        {
            "label": "Review reminder messages",
            "text": "Read the WhatsApp wording patients get, then press Save (keeping the standard wording is fine).",
            "url": reverse("reminders:templates"),
            "done": flags["has_own_wording"] or flags["saved_wording"],
        },
    ]
    if all(step["done"] for step in steps):
        return []
    return steps


@require_POST
@clinic_required(roles=OWNER_ONLY)
def hide_setup_checklist(request):
    """The "Hide" button on the owner's "Get your clinic ready" card."""
    clinic = request.clinic
    SetupChecklist.objects.update_or_create(
        clinic=clinic, defaults={"hidden_at": timezone.now(), "hidden_by": request.user}
    )
    log_action(request, Action.UPDATE, clinic, "Hid the setup checklist")
    messages.success(request, "Checklist hidden. Everything on it is in the menu under Clinic.")
    return redirect("core:dashboard")


# --- Missed follow-ups ----------------------------------------------------------------------

def follow_up_message_state(visit):
    """(text, badge class): what happened about the WhatsApp message for a missed follow-up."""
    if visit.message_status == Reminder.Status.SENT:
        if visit.message_sent_at:
            return f"Message sent {format_date(timezone.localtime(visit.message_sent_at), 'j M')}", "badge-success"
        return "Message sent", "badge-success"
    blocker = whatsapp_blocker(visit.patient)
    if blocker:
        return f"{blocker}: call", "badge-muted"
    if visit.message_status == Reminder.Status.PENDING:
        return "Message to send", "badge-warning"
    if visit.message_status == Reminder.Status.SKIPPED:
        return "Message skipped", "badge-muted"
    return "Not messaged", "badge-muted"


@require_safe
@clinic_required
def missed_follow_ups_view(request):
    """Patients who didn't come back when the doctor asked, for staff to phone or book.

    Shows no diagnosis or other clinical text, so every role may open it.
    """
    clinic = request.clinic
    today = timezone.localdate()
    visits = (
        missed_follow_ups(clinic, today)
        .select_related("patient")
        .order_by("-follow_up_date", "patient__full_name", "pk")
    )
    page_obj = Paginator(visits, 25).get_page(request.GET.get("page"))
    for visit in page_obj:
        visit.days_late = (today - visit.follow_up_date).days
        visit.message_text, visit.message_badge = follow_up_message_state(visit)
    context = {
        "page_obj": page_obj,
        "grace_days": overdue_grace_days(clinic),
        "window_days": MISSED_FOLLOW_UP_WINDOW_DAYS,
    }
    return render(request, "core/missed_follow_ups.html", context)


# --- Audit log ------------------------------------------------------------------------------

ACTION_BADGES = {
    Action.VIEW: "badge-info",
    Action.CREATE: "badge-success",
    Action.UPDATE: "badge-primary",
    Action.DELETE: "badge-danger",
    Action.EXPORT: "badge-warning",
    Action.IMPORT: "badge-warning",
    Action.SEND: "badge-success",
    Action.LOGIN: "badge-muted",
    Action.LOGOUT: "badge-muted",
    Action.LOGIN_FAILED: "badge-danger",
}


def readable_type(class_name):
    """'LabResult' -> 'Lab result'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", class_name).capitalize()


class AuditLogFilterForm(forms.Form):
    """Filters for the audit log. Every field is optional; a value that doesn't make sense is ignored."""

    user = forms.ModelChoiceField(queryset=User.objects.none(), required=False, empty_label="Everyone", label="Staff member")
    action = forms.ChoiceField(choices=[("", "All actions"), *AuditLog.Action.choices], required=False)
    date_from = forms.DateField(
        required=False, label="From", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    date_to = forms.DateField(
        required=False, label="To", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )

    def __init__(self, *args, clinic, **kwargs):
        super().__init__(*args, **kwargs)
        # Only people who are (or were) members of this clinic. Someone who has not accepted an
        # invitation yet is left out: their name belongs to them, not to the clinic that invited them.
        self.fields["user"].queryset = (
            User.objects.filter(memberships__clinic=clinic, memberships__accepted_at__isnull=False)
            .distinct()
            .order_by("full_name")
        )


@require_safe
@clinic_required(roles=OWNER_ONLY)
def audit_log(request):
    clinic = request.clinic
    form = AuditLogFilterForm(request.GET or None, clinic=clinic)
    filters = {}
    if form.is_bound:
        form.is_valid()  # invalid values (bad dates, other clinics' users...) are simply left out
        filters = form.cleaned_data

    entries = AuditLog.objects.filter(clinic=clinic).select_related("user").order_by("-created_at", "-pk")
    if filters.get("user"):
        entries = entries.filter(user=filters["user"])
    if filters.get("action"):
        entries = entries.filter(action=filters["action"])
    if filters.get("date_from"):
        entries = entries.filter(created_at__date__gte=filters["date_from"])
    if filters.get("date_to"):
        entries = entries.filter(created_at__date__lte=filters["date_to"])

    page_obj = Paginator(entries, 50).get_page(request.GET.get("page"))
    for entry in page_obj:
        entry.badge = ACTION_BADGES.get(entry.action, "badge-muted")
        entry.object_label = readable_type(entry.object_type) if entry.object_type else ""

    return render(
        request,
        "core/audit_log.html",
        {"form": form, "page_obj": page_obj, "is_filtered": any(filters.values())},
    )


# --- Export & backup ------------------------------------------------------------------------

class ExportForm(forms.Form):
    include_lab_files = forms.BooleanField(
        required=False,
        label="Include lab report files",
        help_text="Adds the uploaded PDFs and photos. The download will be bigger.",
    )


@require_http_methods(["GET", "POST"])
@clinic_required(roles=OWNER_ONLY)
def export_data(request):
    clinic = request.clinic
    if request.method == "POST":
        form = ExportForm(request.POST)
        include_files = form.is_valid() and form.cleaned_data["include_lab_files"]
        export = build_clinic_export(clinic, include_lab_files=include_files)
        log_action(
            request,
            Action.EXPORT,
            clinic,
            "Downloaded full clinic export" + (" with lab files" if include_files else ""),
        )
        return FileResponse(export.file, as_attachment=True, filename=export.filename, content_type="application/zip")

    last_export = (
        AuditLog.objects.filter(clinic=clinic, action=Action.EXPORT).select_related("user").order_by("-created_at").first()
    )
    return render(
        request,
        "core/export.html",
        {"form": ExportForm(), "counts": export_counts(clinic), "last_export": last_export},
    )


# --- Installable app (PWA) ------------------------------------------------------------------

THEME_COLOR = "#0f766e"
BACKGROUND_COLOR = "#f3f6f8"

# Bump SW_VERSION to force every installed copy to refresh its cache. The cache name also
# includes a fingerprint of the cached files, so editing app.css or app.js refreshes it too.
SW_VERSION = "v1"
PRECACHED_STATIC_FILES = [
    "css/app.css",
    "js/app.js",
    "icons/icon.svg",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/maskable-512.png",
]


@require_safe
def manifest(request):
    """Web app manifest: lets staff "install" the app on a phone's home screen. Public (no patient data)."""
    name = settings.PRODUCT_NAME
    data = {
        "id": "/",
        "name": name,
        "short_name": name if len(name) <= 12 else name.split()[0],
        "description": "Patient records, appointments and WhatsApp reminders for your clinic.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": BACKGROUND_COLOR,
        "theme_color": THEME_COLOR,
        "lang": "en",
        "icons": [
            {"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("icons/maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            {"src": static("icons/icon.svg"), "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


def _static_fingerprint():
    """Short hash of the precached files' URLs and contents: changes whenever one of them changes."""
    digest = hashlib.sha256()
    for path in PRECACHED_STATIC_FILES:
        digest.update(static(path).encode())
        found = finders.find(path)
        if found:
            with open(found, "rb") as f:
                digest.update(f.read())
    return digest.hexdigest()[:12]


_cached_fingerprint = lru_cache(maxsize=1)(_static_fingerprint)


@require_safe
def service_worker(request):
    """The service worker script, served from the site root so it can control every page."""
    fingerprint = _static_fingerprint() if settings.DEBUG else _cached_fingerprint()
    precache_urls = [static(path) for path in PRECACHED_STATIC_FILES] + [reverse("core:offline")]
    script = render_to_string(
        "core/service_worker.js",
        {
            "cache_name": json.dumps(f"clinic-records-{SW_VERSION}-{fingerprint}"),
            "offline_url": json.dumps(reverse("core:offline")),
            "precache_urls": json.dumps(precache_urls),
        },
    )
    response = HttpResponse(script, content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"  # browsers must check for a new version on every load
    return response


@require_safe
def offline(request):
    """Shown by the service worker when there is no internet. Public, and contains no patient data."""
    return render(request, "core/offline.html")
