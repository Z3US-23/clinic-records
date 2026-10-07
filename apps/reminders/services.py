"""Reminder generation and message rendering.

How reminders work
------------------
The app *prepares* WhatsApp messages and staff send each one with a single tap
(see `views.SendReminderView` and `channels.WhatsAppLinkChannel`). Three kinds
of reminder are prepared automatically:

* APPOINTMENT - a few days before an appointment (`clinic.appointment_reminder_days`).
* FOLLOW_UP   - a few days before a visit's `follow_up_date` (`clinic.followup_reminder_days`).
* OVERDUE     - the follow-up date passed `clinic.overdue_grace_days` ago and the
                patient has not come back and is not booked to come.

Staff can also write a CUSTOM message to one patient.

A reminder that is no longer needed is marked skipped by the system (skip_reason
"system"). It comes back by itself when it is needed again, for example when a
cancelled booking leaves the follow-up open again or the patient opts back in.
A reminder skipped by staff never comes back.

Public API used by other apps:
    generate_reminders(clinic, today=None) -> int
        Idempotent. Prepares the reminders that are due and tidies stale ones.
    refresh_for_appointment(appointment, rescheduled=False) -> None
        Call after an appointment is created, edited or changes status.
    refresh_for_visit(visit) -> None
        Call after a visit is created or its follow_up_date changes.
    refresh_for_patient(patient) -> None
        Call after a patient's reminders_opt_in or is_archived changes.
    came_back_or_still_coming(day_start) -> Q
        The one rule for "a later appointment means the patient came back or is
        still coming" (used for follow-up reminders and the follow-up status).
    later_visits(...), booked_appointments(...) -> QuerySet
        Subqueries for "came back since this visit" / "booked since this visit",
        shared with the Today page's missed follow-ups (apps.core.services).
    wants_reminders(patient) -> bool
        Opted in and not archived (a missing WhatsApp number does not count).
    overdue_grace_days(clinic) -> int
        Days after a follow-up date before it counts as missed (at least 1).

Other helpers (used by this app's views and tests):
    DEFAULT_TEMPLATES, PLACEHOLDERS, render_message(), get_template_body(),
    MessageBuilder, sample_context(), unknown_placeholders(),
    appointment_is_remindable()
"""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import Exists, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounts.models import Membership
from apps.appointments.models import Appointment
from apps.appointments.scheduling import only_doctor
from apps.clinical.models import Visit

from .models import MessageTemplate, Reminder, ReminderKind

# --- Rules -------------------------------------------------------------------

# Missed follow-ups older than this are not chased any more.
OVERDUE_LOOKBACK_DAYS = 60

# Only appointments in these states get a reminder. ("Wants another time" waits
# until staff reschedule it; arrived / seen / cancelled / did-not-come need none.)
REMINDABLE_STATUSES = (Appointment.Status.SCHEDULED, Appointment.Status.CONFIRMED)

# Appointments in these states mean the patient came (Waiting, Seen), whatever the date.
CAME_BACK_STATUSES = (Appointment.Status.ARRIVED, Appointment.Status.COMPLETED)

# Reminder kinds that belong to a visit's follow-up date.
VISIT_KINDS = (ReminderKind.FOLLOW_UP, ReminderKind.OVERDUE)

# How the system marks a reminder that is no longer needed, and finds it again.
SKIPPED_BY_SYSTEM = {"status": Reminder.Status.SKIPPED, "skip_reason": Reminder.SkipReason.SYSTEM}


def came_back_or_still_coming(day_start):
    """Q for later appointments that make a follow-up reminder unnecessary.

    * Waiting or Seen: the patient came back, whatever the date.
    * Booked or Confirmed, at or after `day_start` (the start of today in the
      clinic's timezone): the patient is still coming. Today's booking counts all
      day, so a patient running late is not sent a "you missed your check-up"
      message while they sit in the waiting room.

    "Wants another time" never counts (the patient turned that time down), and
    neither do cancelled / did-not-come appointments or old bookings nobody closed.
    """
    return Q(status__in=CAME_BACK_STATUSES) | Q(status__in=REMINDABLE_STATUSES, scheduled_at__gte=day_start)


def appointment_is_remindable(appointment, now=None):
    """Can a reminder still be sent for this appointment? Booked or Confirmed, and still to come."""
    now = now or timezone.now()
    return appointment.status in REMINDABLE_STATUSES and appointment.scheduled_at > now


def overdue_grace_days(clinic):
    """Days after the follow-up date before it counts as missed.

    At least 1, so a patient is never told they missed a check-up on the day it is due.
    """
    return max(1, clinic.overdue_grace_days)


# Wording used when a value is missing.
DOCTOR_FALLBACK = "the doctor"
CLINIC_PHONE_FALLBACK = "the clinic"

# --- Message templates -------------------------------------------------------

DEFAULT_TEMPLATES = {
    ReminderKind.APPOINTMENT: (
        "Hello {first_name}, this is a reminder of your appointment at {clinic_name} "
        "with {doctor_name} on {date} at {time}.\n"
        "\n"
        "Tap to confirm or ask for another time: {confirm_link}\n"
        "\n"
        "Questions? Call {clinic_phone}."
    ),
    ReminderKind.FOLLOW_UP: (
        "Hello {first_name}, it is almost time to come back to {clinic_name} "
        "for your follow-up check-up, around {date}.\n"
        "\n"
        "Please reply to this message or call {clinic_phone} to book a time that suits you."
    ),
    ReminderKind.OVERDUE: (
        "Hello {first_name}, your follow-up check-up at {clinic_name} was due on {date}. "
        "We hope you are feeling well.\n"
        "\n"
        "Coming back helps {doctor_name} check that your treatment is working. "
        "Please reply to this message or call {clinic_phone} to book a time."
    ),
    ReminderKind.CUSTOM: "Hello {first_name}, ",
}

# The only names that are ever replaced in a template, with help text for the editor.
PLACEHOLDERS = {
    "patient_name": "Patient's full name",
    "first_name": "Patient's first name",
    "clinic_name": "Your clinic's name",
    "clinic_phone": "Your clinic's phone number (from Clinic settings)",
    "doctor_name": 'The doctor\'s name, e.g. "Dr. Sana Iqbal" ("the doctor" if none is chosen)',
    "date": "Appointment date, follow-up date, or the date the follow-up was due",
    "time": "Appointment time (appointment reminders only)",
    "confirm_link": "Link the patient taps to confirm or ask for another time (appointment reminders only)",
}

# A placeholder is exactly "{" + lowercase name + "}". Nothing else is touched.
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
# Anything that looks like a placeholder, for warnings in the template editor.
_BRACED_RE = re.compile(r"\{[^{}\n]{0,40}\}")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def render_message(body, context):
    """Fill the {placeholders} in a template typed by clinic staff.

    Safe by design (unlike str.format): only the plain names in PLACEHOLDERS are
    replaced. Unknown names ({patient}), attribute or index access
    ({first_name.__class__}, {first_name[0]}), format specs ({date:>10}) and stray
    braces are left exactly as typed. Never raises.
    """
    if not body:
        return ""

    def replace(match):
        name = match.group(1)
        if name in PLACEHOLDERS and name in context:
            value = context[name]
            return "" if value is None else str(value)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, str(body))


def unknown_placeholders(body):
    """Things in braces that will NOT be filled in, e.g. ['{patient}'] (for editor warnings)."""
    found = []
    for token in _BRACED_RE.findall(body or ""):
        name = token[1:-1]
        if name not in PLACEHOLDERS and token not in found:
            found.append(token)
    return found


def format_date(value):
    """date -> 'Tue 7 Oct' (short and clear on a phone)."""
    return f"{_WEEKDAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]}"


def format_time(value):
    """time or datetime -> '3:30 pm'."""
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'am' if value.hour < 12 else 'pm'}"


def get_template_body(clinic, kind, language="en"):
    """The clinic's own wording for this kind of message, or the built-in default."""
    template = MessageTemplate.objects.filter(clinic=clinic, kind=kind, language=language).first()
    if template and template.body.strip():
        return template.body
    return DEFAULT_TEMPLATES[kind]


class MessageBuilder:
    """Writes reminder messages for one clinic.

    Loads the clinic's templates and doctors' names once, so generating many
    reminders costs two queries, not two per reminder.
    """

    def __init__(self, clinic, language="en"):
        self.clinic = clinic
        self.language = language
        self._bodies = None
        self._doctor_names = None
        self._only_doctor = None
        self._only_doctor_loaded = False

    def body(self, kind):
        if self._bodies is None:
            self._bodies = dict(
                MessageTemplate.objects.filter(clinic=self.clinic, language=self.language).values_list("kind", "body")
            )
        custom = self._bodies.get(kind, "")
        return custom if custom.strip() else DEFAULT_TEMPLATES[kind]

    def doctor_name(self, user):
        """'Dr. Bilal Hussain' (title from the clinic membership) or 'the doctor'."""
        if user is None:
            return DOCTOR_FALLBACK
        if self._doctor_names is None:
            self._doctor_names = {
                m.user_id: m.display_name
                for m in Membership.objects.filter(clinic=self.clinic).select_related("user")
            }
        return self._doctor_names.get(user.pk) or user.full_name or DOCTOR_FALLBACK

    def solo_doctor(self):
        """The clinic's only doctor (a one-doctor clinic), or None. Looked up once, when first needed.

        Older appointments booked for "any doctor" in a one-doctor clinic are with that doctor,
        so their reminder can name them instead of saying "the doctor".
        """
        if not self._only_doctor_loaded:
            self._only_doctor = only_doctor(self.clinic)
            self._only_doctor_loaded = True
        return self._only_doctor

    # Contexts: the values for each placeholder.

    def base_context(self, patient):
        return {
            "patient_name": patient.full_name,
            "first_name": patient.first_name,
            "clinic_name": self.clinic.name,
            "clinic_phone": (self.clinic.phone or "").strip() or CLINIC_PHONE_FALLBACK,
            "doctor_name": DOCTOR_FALLBACK,
            "date": "",
            "time": "",
            "confirm_link": "",
        }

    def appointment_context(self, appointment):
        local = timezone.localtime(appointment.scheduled_at, ZoneInfo(self.clinic.timezone))
        context = self.base_context(appointment.patient)
        context.update(
            doctor_name=self.doctor_name(appointment.doctor or self.solo_doctor()),
            date=format_date(local.date()),
            time=format_time(local),
            confirm_link=appointment.get_confirm_url(),
        )
        return context

    def visit_context(self, visit):
        context = self.base_context(visit.patient)
        context.update(
            doctor_name=self.doctor_name(visit.doctor),
            date=format_date(visit.follow_up_date) if visit.follow_up_date else "",
        )
        return context

    def custom_context(self, patient, on_date=None):
        context = self.base_context(patient)
        context["date"] = format_date(on_date or _clinic_today(self.clinic))
        return context

    # Finished messages.

    def appointment_message(self, appointment):
        return render_message(self.body(ReminderKind.APPOINTMENT), self.appointment_context(appointment))

    def visit_message(self, visit, kind):
        return render_message(self.body(kind), self.visit_context(visit))

    def custom_message(self, patient):
        return render_message(self.body(ReminderKind.CUSTOM), self.custom_context(patient))


def sample_context(clinic, kind, today=None):
    """Made-up patient details for previewing a template. Touches nothing in the database
    except reading the clinic's doctors (for a realistic doctor name)."""
    today = today or _clinic_today(clinic)
    doctor = (
        Membership.objects.filter(clinic=clinic, is_active=True, role__in=Membership.CLINICAL_ROLES)
        .select_related("user")
        .order_by("created_at")
        .first()
    )
    dates = {
        ReminderKind.APPOINTMENT: today + timedelta(days=1),
        ReminderKind.FOLLOW_UP: today + timedelta(days=2),
        ReminderKind.OVERDUE: today - timedelta(days=5),
        ReminderKind.CUSTOM: today,
    }
    return {
        "patient_name": "Ayesha Khan",
        "first_name": "Ayesha",
        "clinic_name": clinic.name,
        "clinic_phone": (clinic.phone or "").strip() or CLINIC_PHONE_FALLBACK,
        "doctor_name": doctor.display_name if doctor else DOCTOR_FALLBACK,
        "date": format_date(dates.get(kind, today)),
        "time": format_time(time(10, 30)) if kind == ReminderKind.APPOINTMENT else "",
        "confirm_link": f"{settings.SITE_URL}/c/Xy7kP2qLm9/" if kind == ReminderKind.APPOINTMENT else "",
    }


# --- Small helpers -----------------------------------------------------------


def _clinic_tz(clinic):
    return ZoneInfo(clinic.timezone)


def _clinic_today(clinic):
    return timezone.localdate(timezone.now(), _clinic_tz(clinic))


def _start_of_day(day):
    """Midnight at the start of `day` in the active (clinic) timezone."""
    return timezone.make_aware(datetime.combine(day, time.min))


def wants_reminders(patient):
    """Does this patient get automatic reminders? Opted in and not archived.

    A missing WhatsApp number does not count here: the reminder is still prepared so staff
    see it and can phone instead.
    """
    return patient.reminders_opt_in and not patient.is_archived


def _create_reminder(**fields):
    """Create one reminder unless an identical one already exists. Returns True if created.

    The unique constraints make this safe when two requests generate at the same time.
    """
    try:
        with transaction.atomic():
            Reminder.objects.create(**fields)
    except IntegrityError:
        return False
    return True


def _without_reminder(queryset, kind, link):
    """Leave out the appointments / visits that already have a reminder of this kind.

    `link` is the Reminder field that points at the queryset's model ("appointment"
    or "visit"). A reminder the system skipped does not count: it is needed again,
    so it is brought back instead of creating a second one (its id is noted as
    `skipped_reminder_id`). Pending, sent and staff-skipped reminders do count, so
    nothing is ever prepared twice.
    """
    existing = Reminder.objects.filter(kind=kind, **{link: OuterRef("pk")})
    return queryset.filter(~Exists(existing.exclude(**SKIPPED_BY_SYSTEM))).annotate(
        skipped_reminder_id=Subquery(existing.filter(**SKIPPED_BY_SYSTEM).values("pk")[:1])
    )


def _prepare_reminder(skipped_reminder_id, **fields):
    """Create a reminder, or bring back the one the system skipped earlier (with a
    fresh message and day). Returns 1 if a reminder is now waiting to be sent, else 0."""
    if skipped_reminder_id:
        return Reminder.objects.filter(pk=skipped_reminder_id, **SKIPPED_BY_SYSTEM).update(
            status=Reminder.Status.PENDING, skip_reason="", due_date=fields["due_date"], message=fields["message"]
        )
    return int(_create_reminder(**fields))


def _appointment_due_date(appointment, today, clinic):
    """Send `appointment_reminder_days` before the appointment, but never in the past."""
    appointment_day = timezone.localtime(appointment.scheduled_at).date()
    return max(today, appointment_day - timedelta(days=clinic.appointment_reminder_days))


def _appointment_in_window(appointment, today, now, clinic):
    """Still to come, and its (local) date is within the reminder window starting today."""
    appointment_day = timezone.localtime(appointment.scheduled_at).date()
    last_day = today + timedelta(days=clinic.appointment_reminder_days)
    return appointment.scheduled_at > now and today <= appointment_day <= last_day


def later_visits(patient_ref, visit_date_ref, visit_pk_ref):
    """Visits of the same patient after the given one (subquery for Exists)."""
    return Visit.objects.filter(patient_id=patient_ref).filter(
        Q(visit_date__gt=visit_date_ref) | Q(visit_date=visit_date_ref, pk__gt=visit_pk_ref)
    )


def booked_appointments(patient_ref, visit_date_ref, own_appointment_ref, day_start=None):
    """Appointments that make a follow-up reminder unnecessary (subquery for Exists).

    After the visit, attended or still to come (see came_back_or_still_coming),
    and not the appointment the visit itself came from. `day_start` is the start of
    "today"; by default today in the active timezone (the clinic's, inside
    generate_reminders and on staff pages). The Today page uses this too
    (apps.core.services.missed_follow_ups).
    """
    if day_start is None:
        day_start = _start_of_day(timezone.localdate())
    return (
        Appointment.objects.filter(patient_id=patient_ref, scheduled_at__gt=visit_date_ref)
        .filter(came_back_or_still_coming(day_start))
        .exclude(pk=Coalesce(own_appointment_ref, Value(0), output_field=models.BigIntegerField()))
    )


def _visits_awaiting_follow_up(clinic, today):
    """Visits with a follow-up date whose patient has not come back and is not booked to come."""
    day_start = _start_of_day(today)
    return (
        Visit.objects.filter(
            clinic=clinic,
            follow_up_date__isnull=False,
            patient__reminders_opt_in=True,
            patient__is_archived=False,
        )
        .filter(~Exists(later_visits(OuterRef("patient_id"), OuterRef("visit_date"), OuterRef("pk"))))
        .filter(
            ~Exists(
                booked_appointments(
                    OuterRef("patient_id"), OuterRef("visit_date"), OuterRef("appointment_id"), day_start
                )
            )
        )
    )


# --- Generation ----------------------------------------------------------------


def generate_reminders(clinic, today=None):
    """Prepare the reminders that are due for one clinic. Returns how many were prepared
    (new ones, plus ones the system had skipped that are needed again).

    Idempotent: running it twice prepares nothing the second time. Cheap enough
    to run on every dashboard load (a fixed number of queries, plus one write per
    prepared reminder). Also marks pending reminders that are no longer needed as
    skipped (it never deletes or changes sent ones).

    The tidy rules are the exact opposite of the preparation rules, so a reminder
    never flips between "To send" and "Skipped" from one run to the next.

    `today` defaults to the clinic's local date.
    """
    with timezone.override(_clinic_tz(clinic)):
        now = timezone.now()
        if today is None:
            today = timezone.localdate(now)
        builder = MessageBuilder(clinic)
        created = _prepare_appointment_reminders(clinic, today, now, builder)
        created += _prepare_follow_up_reminders(clinic, today, builder)
        created += _prepare_overdue_reminders(clinic, today, builder)
        _tidy_stale_reminders(clinic, today, now)
    return created


def _prepare_appointment_reminders(clinic, today, now, builder):
    last_day = today + timedelta(days=clinic.appointment_reminder_days)
    appointments = _without_reminder(
        Appointment.objects.filter(
            clinic=clinic,
            status__in=REMINDABLE_STATUSES,
            scheduled_at__gt=now,
            scheduled_at__gte=_start_of_day(today),
            scheduled_at__lt=_start_of_day(last_day + timedelta(days=1)),
            patient__reminders_opt_in=True,
            patient__is_archived=False,
        ),
        ReminderKind.APPOINTMENT,
        "appointment",
    ).select_related("patient", "doctor")
    prepared = 0
    for appointment in appointments:
        prepared += _prepare_reminder(
            appointment.skipped_reminder_id,
            clinic=clinic,
            patient=appointment.patient,
            appointment=appointment,
            kind=ReminderKind.APPOINTMENT,
            due_date=_appointment_due_date(appointment, today, clinic),
            message=builder.appointment_message(appointment),
        )
    return prepared


def _prepare_follow_up_reminders(clinic, today, builder):
    days = clinic.followup_reminder_days
    visits = _without_reminder(
        _visits_awaiting_follow_up(clinic, today)
        .filter(follow_up_date__gte=today, follow_up_date__lte=today + timedelta(days=days))
        # A missed-follow-up message already replaced this one (see _tidy_stale_reminders).
        .filter(~Exists(Reminder.objects.filter(visit=OuterRef("pk"), kind=ReminderKind.OVERDUE))),
        ReminderKind.FOLLOW_UP,
        "visit",
    ).select_related("patient", "doctor")
    prepared = 0
    for visit in visits:
        prepared += _prepare_reminder(
            visit.skipped_reminder_id,
            clinic=clinic,
            patient=visit.patient,
            visit=visit,
            kind=ReminderKind.FOLLOW_UP,
            due_date=max(today, visit.follow_up_date - timedelta(days=days)),
            message=builder.visit_message(visit, ReminderKind.FOLLOW_UP),
        )
    return prepared


def _prepare_overdue_reminders(clinic, today, builder):
    visits = _without_reminder(
        _visits_awaiting_follow_up(clinic, today).filter(
            follow_up_date__lte=today - timedelta(days=overdue_grace_days(clinic)),
            follow_up_date__gte=today - timedelta(days=OVERDUE_LOOKBACK_DAYS),
        ),
        ReminderKind.OVERDUE,
        "visit",
    ).select_related("patient", "doctor")
    prepared = 0
    for visit in visits:
        prepared += _prepare_reminder(
            visit.skipped_reminder_id,
            clinic=clinic,
            patient=visit.patient,
            visit=visit,
            kind=ReminderKind.OVERDUE,
            due_date=today,
            message=builder.visit_message(visit, ReminderKind.OVERDUE),
        )
    return prepared


def _tidy_stale_reminders(clinic, today, now):
    """Mark pending reminders that are no longer needed as skipped by the system. Returns how many.

    Each rule is the opposite of a preparation rule above: if the reason goes away,
    the next run brings the reminder back.
    """
    pending = Reminder.objects.filter(clinic=clinic, status=Reminder.Status.PENDING)
    patient_left = Q(patient__reminders_opt_in=False) | Q(patient__is_archived=True)
    count = 0

    # Appointment reminders: cancelled, moved to another status, or the time has passed.
    count += (
        pending.filter(kind=ReminderKind.APPOINTMENT)
        .filter(~Q(appointment__status__in=REMINDABLE_STATUSES) | Q(appointment__scheduled_at__lte=now) | patient_left)
        .update(**SKIPPED_BY_SYSTEM)
    )

    visit_reminders = pending.filter(kind__in=VISIT_KINDS)
    # The patient opted out or was archived, or the doctor removed the follow-up date.
    count += visit_reminders.filter(patient_left | Q(visit__follow_up_date__isnull=True)).update(**SKIPPED_BY_SYSTEM)
    # The patient came back (a later visit), or is booked to come.
    count += visit_reminders.filter(
        Exists(later_visits(OuterRef("patient_id"), OuterRef("visit__visit_date"), OuterRef("visit_id")))
    ).update(**SKIPPED_BY_SYSTEM)
    count += visit_reminders.filter(
        Exists(
            booked_appointments(
                OuterRef("patient_id"), OuterRef("visit__visit_date"), OuterRef("visit__appointment_id"),
                _start_of_day(today),
            )
        )
    ).update(**SKIPPED_BY_SYSTEM)
    # A "missed follow-up" message replaces the "follow-up due" one that was never sent.
    count += visit_reminders.filter(
        kind=ReminderKind.FOLLOW_UP,
        visit__reminders__kind=ReminderKind.OVERDUE,
    ).update(**SKIPPED_BY_SYSTEM)
    # Too old to chase.
    count += visit_reminders.filter(
        kind=ReminderKind.OVERDUE,
        visit__follow_up_date__lt=today - timedelta(days=OVERDUE_LOOKBACK_DAYS),
    ).update(**SKIPPED_BY_SYSTEM)
    return count


# --- Keeping reminders in step with appointments, visits and patients ---------------


def refresh_for_appointment(appointment, rescheduled=False):
    """Keep an appointment's reminder in step after it is booked, edited or changes status.

    * Cancelled, did not come, seen (or any state that needs no reminder), or the
      patient opted out: a pending reminder is marked skipped (by the system).
    * Booked or Confirmed again (e.g. "Waiting" tapped by mistake, then undone) and
      the system had skipped the reminder: it comes back, rewritten, if the
      appointment is inside the reminder window; otherwise generation brings it
      back on the right day. A reminder skipped by staff stays skipped.
    * rescheduled=True and a reminder exists: it is rewritten for the new time and
      set back to "To send" (even if the old one was sent) when the new time is
      inside the reminder window; otherwise it is removed so that generation
      prepares it again on the right day.
    * Otherwise the reminder is created now if the appointment is already inside
      the reminder window.
    """
    clinic = appointment.clinic
    with timezone.override(_clinic_tz(clinic)):
        now = timezone.now()
        today = timezone.localdate(now)
        reminder = Reminder.objects.filter(kind=ReminderKind.APPOINTMENT, appointment=appointment).first()

        if appointment.status not in REMINDABLE_STATUSES or not wants_reminders(appointment.patient):
            if reminder is not None and reminder.status == Reminder.Status.PENDING:
                reminder.status = Reminder.Status.SKIPPED
                reminder.skip_reason = Reminder.SkipReason.SYSTEM
                reminder.save(update_fields=["status", "skip_reason"])
            return

        in_window = _appointment_in_window(appointment, today, now, clinic)

        if reminder is not None:
            skipped_by_system = (
                reminder.status == Reminder.Status.SKIPPED and reminder.skip_reason == Reminder.SkipReason.SYSTEM
            )
            if not (rescheduled or skipped_by_system):
                return
            if in_window:
                reminder.message = MessageBuilder(clinic).appointment_message(appointment)
                reminder.due_date = _appointment_due_date(appointment, today, clinic)
                reminder.status = Reminder.Status.PENDING
                reminder.skip_reason = ""
                reminder.sent_at = None
                reminder.sent_by = None
                reminder.save(update_fields=["message", "due_date", "status", "skip_reason", "sent_at", "sent_by"])
            elif rescheduled:
                reminder.delete()
            # Otherwise it stays skipped until generation brings it back inside the window.
            return

        if in_window:
            _create_reminder(
                clinic=clinic,
                patient=appointment.patient,
                appointment=appointment,
                kind=ReminderKind.APPOINTMENT,
                due_date=_appointment_due_date(appointment, today, clinic),
                message=MessageBuilder(clinic).appointment_message(appointment),
            )


def refresh_for_visit(visit):
    """Keep follow-up reminders in step after a visit is saved.

    * A visit makes the pending follow-up / missed-follow-up reminders of the
      patient's EARLIER visits unnecessary: they are marked skipped (by the system).
    * This visit's own pending reminders are removed if its follow-up date was
      changed or cleared, so that generation prepares them again with the right date.
    """
    clinic = visit.clinic
    with timezone.override(_clinic_tz(clinic)):
        today = timezone.localdate()
        pending = Reminder.objects.filter(
            patient_id=visit.patient_id, kind__in=VISIT_KINDS, status=Reminder.Status.PENDING
        )
        earlier = Q(visit__visit_date__lt=visit.visit_date) | Q(
            visit__visit_date=visit.visit_date, visit_id__lt=visit.pk
        )
        pending.filter(earlier).exclude(visit_id=visit.pk).update(**SKIPPED_BY_SYSTEM)

        for reminder in pending.filter(visit_id=visit.pk):
            if not _still_fits_follow_up_date(reminder, visit, today):
                reminder.delete()


def refresh_for_patient(patient):
    """Keep a patient's reminders in step after their reminders_opt_in or is_archived changes.

    * Opted out or archived: their pending appointment / follow-up / missed-follow-up
      reminders are marked skipped (by the system) at once, so they leave "To send"
      and the menu count straight away. When archived, pending custom messages are
      skipped too: they can no longer be sent. (An opted-out patient's custom
      messages stay: staff wrote them knowing the patient expects them.)
    * Opted in and active: generation runs now, so reminders the system skipped
      come back at once if they are still needed.
    """
    if wants_reminders(patient):
        generate_reminders(patient.clinic)
        return
    pending = Reminder.objects.filter(patient=patient, status=Reminder.Status.PENDING)
    if not patient.is_archived:
        pending = pending.exclude(kind=ReminderKind.CUSTOM)
    pending.update(**SKIPPED_BY_SYSTEM)


# A date written by format_date(), e.g. "Tue 7 Oct".
_MESSAGE_DATE_RE = re.compile(r"\b(?:%s) \d{1,2} (?:%s)\b" % ("|".join(_WEEKDAYS), "|".join(_MONTHS)))


def _still_fits_follow_up_date(reminder, visit, today):
    """Was this pending reminder prepared for the visit's CURRENT follow-up date?

    The reminder does not store the date it was written for, so we check what we can:
    its due day must still make sense for that date, and any date written in the
    message must be that date. (Staff may have edited the wording; that is fine as
    long as the date still matches.)
    """
    follow_up = visit.follow_up_date
    if follow_up is None:
        return False
    clinic = visit.clinic

    if reminder.kind == ReminderKind.FOLLOW_UP:
        # Prepared at most `followup_reminder_days` before the date, and never after it.
        latest_follow_up = reminder.due_date + timedelta(days=clinic.followup_reminder_days)
        if not reminder.due_date <= follow_up <= latest_follow_up:
            return False
    elif reminder.kind == ReminderKind.OVERDUE:
        # Only right while the follow-up is still overdue.
        if follow_up > today - timedelta(days=overdue_grace_days(clinic)):
            return False

    dates_in_message = _MESSAGE_DATE_RE.findall(reminder.message)
    return not dates_in_message or format_date(follow_up) in dates_in_message
