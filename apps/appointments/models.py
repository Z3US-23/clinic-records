import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def generate_confirm_token():
    return secrets.token_urlsafe(24)


class Appointment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CONFIRMED = "confirmed", "Confirmed by patient"
        RESCHEDULE_REQUESTED = "reschedule", "Wants another time"
        ARRIVED = "arrived", "Arrived / waiting"
        COMPLETED = "completed", "Seen"
        NO_SHOW = "no_show", "Did not come"
        CANCELLED = "cancelled", "Cancelled"

    # Appointments still expected to happen.
    ACTIVE_STATUSES = (Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED, Status.ARRIVED)

    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="appointments")
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="appointments")
    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="appointments", help_text="Leave empty for 'any available doctor'",
    )
    scheduled_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=15)
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)

    # Secret for the patient's public "confirm / ask for another time" page (no login).
    confirm_token = models.CharField(max_length=64, unique=True, default=generate_confirm_token, editable=False)
    patient_responded_at = models.DateTimeField(null=True, blank=True)
    patient_note = models.CharField(max_length=255, blank=True, help_text="Message left by the patient")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduled_at"]
        indexes = [
            models.Index(fields=["clinic", "scheduled_at"]),
            models.Index(fields=["clinic", "status"]),
        ]

    def __str__(self):
        return f"{self.patient.full_name} – {timezone.localtime(self.scheduled_at):%d %b %Y %H:%M}"

    @property
    def ends_at(self):
        return self.scheduled_at + timedelta(minutes=self.duration_minutes)

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def get_confirm_url(self):
        """Absolute public link sent to the patient inside the WhatsApp reminder."""
        return settings.SITE_URL + reverse("public:confirm", args=[self.confirm_token])
