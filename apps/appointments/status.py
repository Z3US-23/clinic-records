"""Appointment statuses: short labels for staff, which changes are allowed, and the quick buttons.

Kept in one place so the day view, the edit form and the status endpoint all agree.
"""

from .models import Appointment

Status = Appointment.Status

# Short, plain words shown on chips and badges (the model's labels are a bit longer).
STATUS_LABELS = {
    Status.SCHEDULED: "Booked",
    Status.CONFIRMED: "Confirmed",
    Status.RESCHEDULE_REQUESTED: "Wants another time",
    Status.ARRIVED: "Waiting",
    Status.COMPLETED: "Seen",
    Status.NO_SHOW: "Did not come",
    Status.CANCELLED: "Cancelled",
}

# Order of the status chips on the day view.
STATUS_ORDER = list(STATUS_LABELS)

# Which status an appointment may move to from its current one.
# Anything not listed is refused (e.g. a cancelled appointment cannot become "arrived":
# book a new appointment instead).
_ALL_STATUSES = {
    Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED,
    Status.ARRIVED, Status.COMPLETED, Status.NO_SHOW, Status.CANCELLED,
}
ALLOWED_TRANSITIONS = {
    Status.SCHEDULED: _ALL_STATUSES - {Status.SCHEDULED},
    Status.CONFIRMED: _ALL_STATUSES - {Status.CONFIRMED},
    Status.RESCHEDULE_REQUESTED: _ALL_STATUSES - {Status.RESCHEDULE_REQUESTED},
    # Waiting: seen, left without being seen, cancelled, or "not here yet" (undo a wrong tap).
    Status.ARRIVED: {Status.SCHEDULED, Status.COMPLETED, Status.NO_SHOW, Status.CANCELLED},
    # Seen by mistake: put the patient back in the waiting room.
    Status.COMPLETED: {Status.ARRIVED},
    # Marked "did not come" but turned up late, or marked by mistake.
    Status.NO_SHOW: {Status.ARRIVED, Status.SCHEDULED},
    Status.CANCELLED: set(),
}

# Appointments that have not happened yet (the patient is not here and was not seen).
# Only these must be booked in the future, and only these can get a reminder.
UPCOMING_STATUSES = (Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED)

# Statuses a patient can still answer on the public confirmation page.
PATIENT_CAN_RESPOND = UPCOMING_STATUSES


def label(status):
    """Short staff-facing label for a status value."""
    return STATUS_LABELS.get(status, str(status))


def can_change(current, new):
    """True if an appointment in `current` status may be moved to `new`."""
    return new in ALLOWED_TRANSITIONS.get(current, set())


def allowed_choices(current):
    """(value, label) pairs for the edit form: the current status plus the ones it may move to."""
    allowed = ALLOWED_TRANSITIONS.get(current, set()) | {current}
    return [(value, STATUS_LABELS[value]) for value in STATUS_ORDER if value in allowed]


class QuickAction:
    """One button on the schedule, e.g. "Arrived". Posts `status` to appointments:set_status."""

    def __init__(self, status, text, style="btn-secondary", confirm=""):
        self.status = status
        self.text = text
        self.style = style
        self.confirm = confirm


_ARRIVED = QuickAction(Status.ARRIVED, "Arrived", "btn-primary")
_SEEN = QuickAction(Status.COMPLETED, "Seen", "btn-primary")
_NO_SHOW = QuickAction(Status.NO_SHOW, "Did not come")
_CANCEL = QuickAction(Status.CANCELLED, "Cancel", "btn-danger-outline", confirm="Cancel this appointment?")
_CAME_LATE = QuickAction(Status.ARRIVED, "Arrived late")

# The buttons shown on each row of the day view, by current status.
QUICK_ACTIONS = {
    Status.SCHEDULED: [_ARRIVED, _NO_SHOW, _CANCEL],
    Status.CONFIRMED: [_ARRIVED, _NO_SHOW, _CANCEL],
    Status.RESCHEDULE_REQUESTED: [_ARRIVED, _NO_SHOW, _CANCEL],
    Status.ARRIVED: [_SEEN],
    Status.COMPLETED: [],
    Status.NO_SHOW: [_CAME_LATE],
    Status.CANCELLED: [],
}
