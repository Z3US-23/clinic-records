"""Queries behind the Today page: the patients someone at the front desk should act on.

    day_bounds(day)                       start and end of a day, in the clinic's timezone
    reschedule_requests(clinic, today)    "I need another time" answers nobody has dealt with yet
    missed_follow_ups(clinic, today)      follow-up dates that passed: the patient neither came back nor booked

Every query is limited to one clinic.
"""

from datetime import datetime, time, timedelta

from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.clinical.models import Visit
from apps.reminders.models import Reminder

# The reminders app decides when a patient "came back" (a later visit) and "booked since" (a later
# appointment they came to, or one still to come; "wants another time" and stale bookings don't count).
# Using its subqueries keeps the Today page and the missed-follow-up WhatsApp messages in agreement.
from apps.reminders.services import booked_appointments, later_visits, overdue_grace_days

# A missed follow-up stays on the list this long after its date. (WhatsApp messages about it stop
# after 60 days, but staff can still phone the patient.)
MISSED_FOLLOW_UP_WINDOW_DAYS = 180


def day_bounds(day):
    """Start and end of a calendar day as aware datetimes, in the clinic's timezone (set by the middleware)."""
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def reschedule_requests(clinic, today):
    """Appointments from today onwards whose patient asked for another time, soonest first.

    Nothing needs to clear this list by hand: moving the date, time or doctor sets the appointment
    back to Booked, and Cancel / Did not come / Arrived change its status too. Today's requests stay
    on it even once their time has passed, until someone deals with them.
    """
    day_start, _ = day_bounds(today)
    return (
        Appointment.objects.filter(
            clinic=clinic, status=Appointment.Status.RESCHEDULE_REQUESTED, scheduled_at__gte=day_start
        )
        .select_related("patient", "doctor")
        .order_by("scheduled_at", "pk")
    )


def missed_follow_ups(clinic, today):
    """Visits whose follow-up date passed (by the clinic's grace days) with no later visit, and no
    later appointment the patient came to or is still booked for.

    Only a patient's latest visit can qualify, so each patient appears once. Unlike the WhatsApp
    "missed follow-up" reminders, this also lists patients who don't want reminders or have no
    WhatsApp (staff can phone them) and patients who were already messaged.

    Each visit is annotated with `message_status` and `message_sent_at`: its missed-follow-up
    reminder's status and sending time (None when there is no such reminder).
    """
    overdue_reminder = Reminder.objects.filter(visit=OuterRef("pk"), kind=Reminder.Kind.OVERDUE).order_by("-pk")
    day_start, _ = day_bounds(today)
    return (
        Visit.objects.filter(
            clinic=clinic,
            follow_up_date__lte=today - timedelta(days=overdue_grace_days(clinic)),
            follow_up_date__gte=today - timedelta(days=MISSED_FOLLOW_UP_WINDOW_DAYS),
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
        .annotate(
            message_status=Subquery(overdue_reminder.values("status")[:1]),
            message_sent_at=Subquery(overdue_reminder.values("sent_at")[:1]),
        )
    )
