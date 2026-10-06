"""Template helpers for appointments. Other apps (e.g. the dashboard) may use them too:

    {% load appointment_tags %}
    {{ appointment.status|status_label }}        -> "Did not come"
    {% appointment_actions appointment %}       -> the quick status buttons (Arrived, Seen, ...)
"""

from django import template

from apps.appointments import status as appt_status

register = template.Library()


@register.filter
def status_label(value):
    """Short staff-facing label for an appointment status value."""
    return appt_status.label(str(value))


@register.inclusion_tag("appointments/includes/status_actions.html", takes_context=True)
def appointment_actions(context, appointment, next_url=None):
    """POST buttons for the status changes that make sense right now.

    After the change the user comes back to `next_url` (default: the current page).
    """
    request = context.get("request")
    if next_url is None and request is not None:
        next_url = request.get_full_path()
    return {
        "appointment": appointment,
        "actions": appt_status.QUICK_ACTIONS.get(appointment.status, []),
        "next_url": next_url,
        "csrf_token": context.get("csrf_token"),
    }
