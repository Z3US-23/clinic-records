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

    # The patient is not (or no longer) in the clinic: saving with one of these clears `arrived_at`.
    # "Seen" keeps it, so "Seen by mistake" puts the patient back in their old place in the queue.
    NOT_ARRIVED_STATUSES = (
        Status.SCHEDULED, Status.CONFIRMED, Status.RESCHEDULE_REQUESTED, Status.NO_SHOW, Status.CANCELLED,
    )

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

    # When the patient was marked "Arrived" (waiting-room order and token). Set by save(), never by a form.
    arrived_at = models.DateTimeField(null=True, blank=True, editable=False)

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
            # A patient's next / later appointments (patient list, follow-up checks).
            models.Index(fields=["patient", "scheduled_at"]),
        ]

    def __str__(self):
        return f"{self.patient.full_name} – {timezone.localtime(self.scheduled_at):%d %b %Y %H:%M}"

    def save(self, *args, **kwargs):
        """Keep `arrived_at` in step with the status, whichever page changed it.

        * Arrived (status change, walk-in booking, "Arrived late"): the time is noted, once.
          Editing a waiting patient's other details keeps it, so they keep their place.
        * Seen: kept.
        * Booked, confirmed, wants another time, did not come, cancelled: cleared, so a
          patient marked "Arrived" by mistake and then really arriving gets the new time.
        """
        if self.status == self.Status.ARRIVED and self.arrived_at is None:
            self.arrived_at = timezone.now()
        elif self.status in self.NOT_ARRIVED_STATUSES:
            self.arrived_at = None
        # Callers that save only the status, e.g. save(update_fields=["status", "updated_at"]),
        # must write the arrival time too.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "status" in update_fields:
            kwargs["update_fields"] = {*update_fields, "arrived_at"}
        super().save(*args, **kwargs)

    @property
    def ends_at(self):
        return self.scheduled_at + timedelta(minutes=self.duration_minutes)

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def get_confirm_url(self):
        """Absolute public link sent to the patient inside the WhatsApp reminder."""
        return settings.SITE_URL + reverse("public:confirm", args=[self.confirm_token])
