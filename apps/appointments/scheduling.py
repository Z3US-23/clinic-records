"""Date/time helpers for the schedule.

All dates here are the clinic's local dates. The middleware activates the clinic's
timezone for staff pages, so `timezone.localdate()` / `timezone.make_aware()` use it.
"""

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from apps.accounts.models import Membership

from .models import Appointment

# Longest appointment the booking form accepts, in minutes. Also bounds the overlap search.
MAX_DURATION_MINUTES = 8 * 60

# A booking may be up to this far in the past (the receptionist was a bit slow typing).
PAST_GRACE = timedelta(minutes=5)


# Dates outside this range in a URL are treated as invalid (avoids overflow on "9999-12-31" + 1 day).
MIN_YEAR, MAX_YEAR = 1900, 2200


def parse_date(raw, default=None):
    """'2026-10-07' -> date(2026, 10, 7); anything invalid -> `default`."""
    if not raw:
        return default
    try:
        value = date.fromisoformat(str(raw).strip())
    except ValueError:
        return default
    if not MIN_YEAR <= value.year <= MAX_YEAR:
        return default
    return value


def start_of_day(day):
    """Aware datetime for 00:00 on `day` in the current (clinic) timezone."""
    return timezone.make_aware(datetime.combine(day, time.min))


def day_range(first_day, days=1):
    """(start, end) aware datetimes covering `days` whole local days from `first_day`."""
    return start_of_day(first_day), start_of_day(first_day + timedelta(days=days))


def monday_of(day):
    return day - timedelta(days=day.weekday())


def local_date(dt):
    return timezone.localtime(dt).date()


def find_clash(clinic, doctor, start, minutes, exclude_pk=None):
    """The first active appointment of `doctor` that overlaps [start, start + minutes), or None.

    Appointments touching end-to-start (10:00-10:15 and 10:15-10:30) do not clash.
    """
    if doctor is None:
        return None
    end = start + timedelta(minutes=minutes)
    candidates = (
        Appointment.objects.filter(
            clinic=clinic,
            doctor=doctor,
            status__in=Appointment.ACTIVE_STATUSES,
            scheduled_at__lt=end,
            scheduled_at__gt=start - timedelta(minutes=MAX_DURATION_MINUTES),
        )
        .select_related("patient")
        .order_by("scheduled_at")
    )
    if exclude_pk:
        candidates = candidates.exclude(pk=exclude_pk)
    for other in candidates:
        if other.ends_at > start:
            return other
    return None


def doctor_names(clinic):
    """{user_id: "Dr. Bilal Hussain"} for everyone who is (or was) a clinician in this clinic.

    One query; used to show doctor names without hitting Membership for every row.
    Includes inactive memberships so old appointments still show who they were with.
    """
    memberships = Membership.objects.filter(
        clinic=clinic, role__in=Membership.CLINICAL_ROLES
    ).select_related("user")
    return {m.user_id: m.display_name for m in memberships}
