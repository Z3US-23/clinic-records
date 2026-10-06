from django import template

from apps.core.phone import format_phone_display

register = template.Library()

# Status value -> badge colour. Covers appointment, reminder and other statuses.
STATUS_BADGES = {
    # appointments
    "scheduled": "badge-info",
    "confirmed": "badge-success",
    "reschedule": "badge-warning",
    "arrived": "badge-primary",
    "completed": "badge-muted",
    "no_show": "badge-danger",
    "cancelled": "badge-muted",
    # reminders
    "pending": "badge-warning",
    "sent": "badge-success",
    "skipped": "badge-muted",
    # reminder kinds
    "appointment": "badge-info",
    "follow_up": "badge-primary",
    "overdue": "badge-danger",
    "custom": "badge-muted",
    # roles
    "owner": "badge-primary",
    "doctor": "badge-info",
    "receptionist": "badge-muted",
}


@register.filter
def badge_class(value):
    """{{ appointment.status|badge_class }} -> 'badge-success' etc."""
    return STATUS_BADGES.get(str(value), "badge-muted")


@register.filter
def initials(name):
    """'Ayesha Khan' -> 'AK'."""
    parts = [p for p in str(name or "").replace("Dr.", "").split() if p[:1].isalpha()]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@register.filter
def phone_display(number):
    """'923001234567' -> '+92 300 1234567'."""
    return format_phone_display(number)
