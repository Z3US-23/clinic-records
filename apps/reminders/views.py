"""Reminder screens: the list of messages to send, one-tap sending, editing,
custom messages and (owner only) the message templates editor.

Every query is limited to `request.clinic`; another clinic's reminder is a 404.
"""

import logging
import re
from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.core.audit import Action, log_action
from apps.core.permissions import ClinicRequiredMixin, OwnerRequiredMixin
from apps.patients.models import Patient
from apps.patients.services import search_patients

from . import services
from .channels import get_channel
from .forms import CustomReminderForm, MessageTemplateForm, PatientSearchForm, ReminderMessageForm
from .models import MessageTemplate, Reminder, ReminderKind

logger = logging.getLogger(__name__)

# Short words for audit summaries and messages: "Sent follow-up reminder to P-00012".
KIND_WORDS = {
    ReminderKind.APPOINTMENT: "appointment",
    ReminderKind.FOLLOW_UP: "follow-up",
    ReminderKind.OVERDUE: "missed follow-up",
    ReminderKind.CUSTOM: "custom",
}


def _kind_word(reminder_or_kind):
    kind = getattr(reminder_or_kind, "kind", reminder_or_kind)
    return KIND_WORDS.get(kind, "")


def _list_url(tab=None):
    url = reverse("reminders:list")
    return f"{url}?tab={tab}" if tab else url


def _safe_next(request, default):
    """The `next` URL from the form or query string, if it points back into this site."""
    candidate = request.POST.get("next") or request.GET.get("next")
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return default


def _get_reminder(request, pk):
    return get_object_or_404(Reminder.objects.select_related("patient"), pk=pk, clinic=request.clinic)


def _no_number_message(patient):
    return (
        f"{patient.full_name} has no valid WhatsApp number. "
        "Call them instead, or fix the number on their record."
    )


def _send_problem(reminder):
    """Why this reminder can't be opened in WhatsApp, or "" when it can."""
    patient = reminder.patient
    if reminder.status == Reminder.Status.SKIPPED:
        return "This reminder was skipped, so it can't be sent. Write a new message instead."
    if patient.is_archived:
        return f"{patient.full_name}'s record is archived, so reminders can't be sent."
    if get_channel(reminder).url_for(reminder) is None:
        return _no_number_message(patient)
    return ""


def _send_on_whatsapp(request, reminder, back_url):
    """The one-tap send: mark the reminder sent, audit it and redirect to WhatsApp.

    The first send records who sent it and when. "Send again" on a sent reminder
    re-opens WhatsApp but keeps the original sent time (it is still audited).
    When the reminder can't be sent, explains why and goes back to `back_url`.
    """
    problem = _send_problem(reminder)
    if problem:
        messages.error(request, problem)
        return redirect(back_url)

    first_time = Reminder.objects.filter(pk=reminder.pk, status=Reminder.Status.PENDING).update(
        status=Reminder.Status.SENT,
        sent_at=timezone.now(),
        sent_by=request.user,
        channel=Reminder.Channel.WHATSAPP_LINK,
    )
    patient = reminder.patient
    summary = f"Sent {_kind_word(reminder)} reminder to {patient.mrn}"
    if first_time:
        messages.success(request, f"Reminder for {patient.full_name} marked as sent.")
    else:
        summary += " again"
        messages.info(request, f"WhatsApp opened again for {patient.full_name}.")
    log_action(request, Action.SEND, reminder, summary)
    # Only ever the channel's own wa.me link: never a URL taken from the request.
    return HttpResponseRedirect(get_channel(reminder).url_for(reminder))


def _decorate(reminder, today):
    """Extra display values for one row."""
    patient = reminder.patient
    reminder.days_late = (today - reminder.due_date).days if reminder.due_date < today else 0
    reminder.has_whatsapp = bool(patient.whatsapp_number)
    reminder.call_number = re.sub(r"[^\d+]", "", patient.phone or "")
    return reminder


# --- List ----------------------------------------------------------------------


class ReminderListView(ClinicRequiredMixin, View):
    """Reminders in four tabs: To send (default), Coming up, Sent, Skipped."""

    template_name = "reminders/list.html"
    paginate_by = 50
    recent_days = 30

    def tabs(self, today):
        """tab key -> (label, filter, ordering)."""
        since = timezone.now() - timedelta(days=self.recent_days)
        pending = Q(status=Reminder.Status.PENDING)
        return {
            "due": ("To send", pending & Q(due_date__lte=today), ("due_date", "pk")),
            "upcoming": ("Coming up", pending & Q(due_date__gt=today), ("due_date", "pk")),
            "sent": ("Sent", Q(status=Reminder.Status.SENT, sent_at__gte=since), ("-sent_at", "-pk")),
            "skipped": (
                "Skipped",
                Q(status=Reminder.Status.SKIPPED, due_date__gte=today - timedelta(days=self.recent_days)),
                ("-due_date", "-pk"),
            ),
        }

    def get(self, request):
        clinic = request.clinic
        try:
            services.generate_reminders(clinic)
        except Exception:  # never let preparation problems hide reminders that are ready
            logger.exception("Could not prepare reminders for clinic %s", clinic.pk)
            messages.warning(request, "New reminders could not be prepared just now. Please try again later.")

        today = timezone.localdate()
        tabs = self.tabs(today)
        current = request.GET.get("tab")
        if current not in tabs:
            current = "due"

        clinic_reminders = Reminder.objects.filter(clinic=clinic)
        counts = clinic_reminders.aggregate(
            **{key: Count("pk", filter=condition) for key, (_, condition, _) in tabs.items()}
        )
        label, condition, ordering = tabs[current]
        queryset = (
            clinic_reminders.filter(condition)
            .select_related("patient", "sent_by")
            .annotate(appointment_at=F("appointment__scheduled_at"), follow_up_on=F("visit__follow_up_date"))
            .order_by(*ordering)
        )
        page_obj = Paginator(queryset, self.paginate_by).get_page(request.GET.get("page"))
        reminders = [_decorate(reminder, today) for reminder in page_obj]

        context = {
            "tabs": [
                {"key": key, "label": tab_label, "count": counts[key], "url": _list_url(key), "active": key == current}
                for key, (tab_label, _, _) in tabs.items()
            ],
            "current_tab": current,
            "current_label": label,
            "page_obj": page_obj,
            "reminders": reminders,
            "today": today,
            "recent_days": self.recent_days,
            "back_url": request.get_full_path(),
        }
        return render(request, self.template_name, context)


# --- Actions (POST only) -------------------------------------------------------------


class SendReminderView(ClinicRequiredMixin, View):
    """Mark a reminder sent and open WhatsApp with the message ready (302 to wa.me)."""

    http_method_names = ["post"]

    def post(self, request, pk):
        reminder = _get_reminder(request, pk)
        return _send_on_whatsapp(request, reminder, _safe_next(request, _list_url()))


class SkipReminderView(ClinicRequiredMixin, View):
    """Mark a pending reminder as not needed."""

    http_method_names = ["post"]

    def post(self, request, pk):
        reminder = _get_reminder(request, pk)
        back = _safe_next(request, _list_url())

        skipped = Reminder.objects.filter(pk=reminder.pk, status=Reminder.Status.PENDING).update(
            status=Reminder.Status.SKIPPED
        )
        if skipped:
            log_action(
                request, Action.UPDATE, reminder,
                f"Skipped {_kind_word(reminder)} reminder for {reminder.patient.mrn}",
            )
            messages.success(request, f"Reminder for {reminder.patient.full_name} skipped.")
        else:
            messages.info(request, "This reminder was already sent or skipped.")
        return redirect(back)


# --- Edit ----------------------------------------------------------------------------


class ReminderUpdateView(ClinicRequiredMixin, View):
    """Change the wording (or day) of a reminder that has not been sent yet."""

    template_name = "reminders/reminder_form.html"

    def _locked(self, request, reminder):
        """Sent and skipped reminders are read-only."""
        if reminder.status == Reminder.Status.PENDING:
            return None
        if reminder.status == Reminder.Status.SENT:
            messages.info(request, "This reminder was already sent, so it can't be changed.")
            return redirect(_list_url("sent"))
        messages.info(request, "This reminder was skipped, so it can't be changed. Write a new message instead.")
        return redirect(_list_url("skipped"))

    def render_form(self, request, reminder, form):
        return render(
            request,
            self.template_name,
            {
                "reminder": reminder,
                "patient": reminder.patient,
                "form": form,
                "has_whatsapp": bool(reminder.patient.whatsapp_number),
                "next_url": _safe_next(request, ""),
            },
        )

    def get(self, request, pk):
        reminder = _get_reminder(request, pk)
        locked = self._locked(request, reminder)
        if locked:
            return locked
        return self.render_form(request, reminder, ReminderMessageForm(instance=reminder))

    def post(self, request, pk):
        reminder = _get_reminder(request, pk)
        locked = self._locked(request, reminder)
        if locked:
            return locked

        form = ReminderMessageForm(request.POST, instance=reminder)
        if not form.is_valid():
            return self.render_form(request, reminder, form)

        if form.has_changed():
            # Only the edited fields: never overwrite a status another person just changed.
            form.save(commit=False).save(update_fields=form.changed_data)
            log_action(
                request, Action.UPDATE, reminder,
                f"Edited {_kind_word(reminder)} reminder for {reminder.patient.mrn}",
            )

        if request.POST.get("action") == "send":
            return _send_on_whatsapp(request, reminder, _list_url())

        messages.success(request, "Reminder saved.")
        tab = "due" if reminder.due_date <= timezone.localdate() else "upcoming"
        return redirect(_safe_next(request, _list_url(tab)))


# --- Custom message -------------------------------------------------------------------


def _search_patients(clinic, query, limit=20):
    """Active patients matching a name, MR number or phone number (same rules as the patient list)."""
    patients = Patient.objects.filter(clinic=clinic, is_archived=False)
    return list(search_patients(patients, query, clinic.country).order_by("full_name")[:limit])


class CustomReminderCreateView(ClinicRequiredMixin, View):
    """Write a message to one patient: send it now on WhatsApp, or save it for later.

    Without ?patient=<id> it shows a simple patient search first.
    """

    template_name = "reminders/reminder_create.html"
    search_template_name = "reminders/reminder_create_search.html"

    def get_patient(self, request):
        patient_id = request.GET.get("patient", "").strip()
        if not patient_id:
            return None
        if not patient_id.isdigit():
            raise Http404("No such patient.")
        return get_object_or_404(Patient, pk=patient_id, clinic=request.clinic, is_archived=False)

    def search(self, request):
        form = PatientSearchForm(request.GET or None)
        query = form.cleaned_data["q"].strip() if form.is_valid() else ""
        results = _search_patients(request.clinic, query) if query else []
        return render(request, self.search_template_name, {"form": form, "query": query, "results": results})

    def render_form(self, request, patient, form, status=200):
        return render(
            request,
            self.template_name,
            {"patient": patient, "form": form, "has_whatsapp": bool(patient.whatsapp_number)},
            status=status,
        )

    def get(self, request):
        patient = self.get_patient(request)
        if patient is None:
            return self.search(request)
        message = services.MessageBuilder(request.clinic).custom_message(patient)
        form = CustomReminderForm(initial={"message": message})
        return self.render_form(request, patient, form)

    def post(self, request):
        patient = self.get_patient(request)
        if patient is None:
            return redirect("reminders:create")

        form = CustomReminderForm(request.POST)
        if not form.is_valid():
            return self.render_form(request, patient, form)

        send_now = request.POST.get("action") == "send"
        if send_now and not patient.whatsapp_number:
            messages.error(request, _no_number_message(patient))
            return self.render_form(request, patient, form)

        today = timezone.localdate()
        due_date = today if send_now else (form.cleaned_data["due_date"] or today)
        reminder = Reminder.objects.create(
            clinic=request.clinic,
            patient=patient,
            kind=ReminderKind.CUSTOM,
            due_date=due_date,
            message=form.cleaned_data["message"],
            created_by=request.user,
        )
        log_action(request, Action.CREATE, reminder, f"Wrote custom message for {patient.mrn}")

        if send_now:
            return _send_on_whatsapp(request, reminder, _list_url())

        if due_date <= today:
            messages.success(request, f"Message for {patient.full_name} saved under “To send”.")
            return redirect(_list_url("due"))
        messages.success(
            request,
            f"Message for {patient.full_name} saved. It will show under “To send” on {date_format(due_date, 'j M Y')}.",
        )
        return redirect(_list_url("upcoming"))


# --- Message templates (owner only) -------------------------------------------------------


def _kind_help(clinic, kind):
    """When each kind of reminder is prepared, in the clinic's own numbers."""

    def days(n):
        return f"{n} day{'' if n == 1 else 's'}"

    if kind == ReminderKind.APPOINTMENT:
        return f"Prepared {days(clinic.appointment_reminder_days)} before each appointment."
    if kind == ReminderKind.FOLLOW_UP:
        return f"Prepared {days(clinic.followup_reminder_days)} before the follow-up date the doctor set."
    if kind == ReminderKind.OVERDUE:
        return (
            f"Prepared when a follow-up is {days(clinic.overdue_grace_days)} late "
            "and the patient has not come back or booked."
        )
    return "The starting text when you write a message to one patient."


def _template_warnings(kind, body):
    warnings = []
    if kind == ReminderKind.APPOINTMENT and "{confirm_link}" not in body:
        warnings.append(
            "This message has no {confirm_link}, so patients can't confirm or ask for another time with one tap."
        )
    unknown = services.unknown_placeholders(body)
    if unknown:
        warnings.append(
            f"Not recognised: {', '.join(unknown)}. These will be sent exactly as typed. "
            "Check the spelling against the list of placeholders."
        )
    return warnings


class MessageTemplatesView(OwnerRequiredMixin, View):
    """Edit the wording of each kind of reminder, with a preview using made-up details."""

    template_name = "reminders/templates.html"
    language = "en"

    def custom_templates(self, clinic):
        return {t.kind: t for t in MessageTemplate.objects.filter(clinic=clinic, language=self.language)}

    @staticmethod
    def make_form(kind, data=None, initial=None):
        # Four forms share one page, so give each its own HTML ids.
        return MessageTemplateForm(data=data, initial=initial, auto_id=f"id_{kind}_%s", kind=kind)

    def render_page(self, request, bound_form=None):
        """All four templates. `bound_form` is the one just posted (shown with its errors or preview)."""
        clinic = request.clinic
        saved = self.custom_templates(clinic)
        today = timezone.localdate()
        cards, samples = [], {}
        for kind, label in ReminderKind.choices:
            custom = saved.get(kind)
            if bound_form is not None and bound_form.data.get("kind") == kind:
                form, body = bound_form, bound_form.data.get("body", "")
            else:
                body = custom.body if custom and custom.body.strip() else services.DEFAULT_TEMPLATES[kind]
                form = self.make_form(kind, initial={"kind": kind, "body": body})
            samples[kind] = services.sample_context(clinic, kind, today)
            cards.append(
                {
                    "kind": kind,
                    "label": label,
                    "help": _kind_help(clinic, kind),
                    "form": form,
                    "custom": custom,
                    "preview": services.render_message(body, samples[kind]),
                    "warnings": _template_warnings(kind, body),
                }
            )
        context = {"cards": cards, "placeholders": services.PLACEHOLDERS, "samples": samples}
        return render(request, self.template_name, context)

    def get(self, request):
        return self.render_page(request)

    def post(self, request):
        clinic = request.clinic
        kind = request.POST.get("kind")
        if kind not in ReminderKind.values:
            messages.error(request, "Unknown message type.")
            return redirect("reminders:templates")
        action = request.POST.get("action", "save")
        word = _kind_word(kind)

        if action == "reset":
            deleted, _ = MessageTemplate.objects.filter(clinic=clinic, kind=kind, language=self.language).delete()
            if deleted:
                log_action(request, Action.UPDATE, None, f"Reset {word} message template to default")
            messages.success(request, f"The {word} message is back to the standard wording.")
            return redirect(f"{reverse('reminders:templates')}#template-{kind}")

        form = self.make_form(kind, data=request.POST)
        if not form.is_valid() or action == "preview":
            # Show the errors, or the preview of the unsaved text. Nothing is written.
            return self.render_page(request, bound_form=form)

        body = form.cleaned_data["body"]
        if body.strip() == services.DEFAULT_TEMPLATES[kind].strip():
            # Same as the standard wording: store nothing, so future improvements apply.
            MessageTemplate.objects.filter(clinic=clinic, kind=kind, language=self.language).delete()
            template = None
        else:
            template, _ = MessageTemplate.objects.update_or_create(
                clinic=clinic, kind=kind, language=self.language, defaults={"body": body}
            )
        log_action(request, Action.UPDATE, template, f"Updated {word} message template")
        messages.success(request, f"The {word} message was saved. New reminders will use it.")
        for warning in _template_warnings(kind, body):
            messages.warning(request, warning)
        return redirect(f"{reverse('reminders:templates')}#template-{kind}")
