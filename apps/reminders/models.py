from django.conf import settings
from django.db import models
from django.db.models import Q


class ReminderKind(models.TextChoices):
    APPOINTMENT = "appointment", "Appointment reminder"
    FOLLOW_UP = "follow_up", "Follow-up due"
    OVERDUE = "overdue", "Missed follow-up"
    CUSTOM = "custom", "Custom message"


class Reminder(models.Model):
    """A message to a patient, prepared by the system and sent by staff (one tap)."""

    Kind = ReminderKind

    class Status(models.TextChoices):
        PENDING = "pending", "To send"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"

    class Channel(models.TextChoices):
        WHATSAPP_LINK = "whatsapp_link", "WhatsApp (one tap)"
        WHATSAPP_API = "whatsapp_api", "WhatsApp (automatic)"
        SMS = "sms", "SMS"

    class SkipReason(models.TextChoices):
        STAFF = "staff", "Skipped by staff"
        SYSTEM = "system", "No longer needed"

    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="reminders")
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="reminders")
    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.CASCADE, null=True, blank=True, related_name="reminders"
    )
    visit = models.ForeignKey(
        "clinical.Visit", on_delete=models.CASCADE, null=True, blank=True, related_name="reminders"
    )

    kind = models.CharField(max_length=20, choices=ReminderKind.choices)
    due_date = models.DateField(help_text="Day the reminder should go out")
    message = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Who skipped it. A reminder the SYSTEM skipped (e.g. the booking was cancelled, the
    # patient opted out) comes back by itself when it is needed again; one skipped by
    # STAFF never does. Blank otherwise, and on rows skipped before this was recorded
    # (those are treated like staff skips, so they never come back unexpectedly).
    skip_reason = models.CharField(max_length=10, choices=SkipReason.choices, blank=True)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.WHATSAPP_LINK)

    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date", "pk"]
        indexes = [models.Index(fields=["clinic", "status", "due_date"])]
        constraints = [
            # Generation is idempotent: at most one reminder of a kind per appointment / visit.
            models.UniqueConstraint(
                fields=["kind", "appointment"], condition=Q(appointment__isnull=False),
                name="one_reminder_per_kind_per_appointment",
            ),
            models.UniqueConstraint(
                fields=["kind", "visit"], condition=Q(visit__isnull=False),
                name="one_reminder_per_kind_per_visit",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} → {self.patient.full_name} ({self.due_date:%d %b})"


class MessageTemplate(models.Model):
    """A clinic's wording for each kind of reminder. Missing ones fall back to the built-in defaults."""

    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="message_templates")
    kind = models.CharField(max_length=20, choices=ReminderKind.choices)
    language = models.CharField(max_length=10, default="en")
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(fields=["clinic", "kind", "language"], name="one_template_per_kind_language"),
        ]

    def __str__(self):
        return f"{self.clinic} – {self.get_kind_display()} ({self.language})"
