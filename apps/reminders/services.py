"""Reminder generation and message rendering. (Stub — implemented by the reminders feature.)

Public API used by other apps:
    generate_reminders(clinic, today=None) -> int
        Idempotent. Creates the pending reminders that are due and tidies stale ones.
    refresh_for_appointment(appointment, rescheduled=False) -> None
        Call after an appointment is created, edited or changes status.
    refresh_for_visit(visit) -> None
        Call after a visit is created or its follow_up_date changes.
"""


def generate_reminders(clinic, today=None):
    return 0


def refresh_for_appointment(appointment, rescheduled=False):
    return None


def refresh_for_visit(visit):
    return None
