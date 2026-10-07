import os
import uuid

from django.conf import settings
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Visit(models.Model):
    """One consultation: what the doctor found, decided and prescribed."""

    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="visits")
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="visits")
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="visits")
    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.SET_NULL, null=True, blank=True, related_name="visits"
    )
    visit_date = models.DateTimeField(default=timezone.now)

    chief_complaint = models.CharField("presenting complaint", max_length=255)
    history = models.TextField("history", blank=True)
    examination = models.TextField("examination findings", blank=True)

    # Vitals (all optional)
    bp_systolic = models.PositiveSmallIntegerField(
        "BP systolic", null=True, blank=True, validators=[MinValueValidator(40), MaxValueValidator(300)]
    )
    bp_diastolic = models.PositiveSmallIntegerField(
        "BP diastolic", null=True, blank=True, validators=[MinValueValidator(20), MaxValueValidator(200)]
    )
    pulse = models.PositiveSmallIntegerField(
        "pulse (bpm)", null=True, blank=True, validators=[MinValueValidator(20), MaxValueValidator(250)]
    )
    temperature_c = models.DecimalField(
        "temperature (°C)", max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(30), MaxValueValidator(45)],
    )
    weight_kg = models.DecimalField(
        "weight (kg)", max_digits=5, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(0.5), MaxValueValidator(400)],
    )
    height_cm = models.DecimalField(
        "height (cm)", max_digits=5, decimal_places=1, null=True, blank=True,
        validators=[MinValueValidator(30), MaxValueValidator(250)],
    )
    spo2 = models.PositiveSmallIntegerField(
        "SpO₂ (%)", null=True, blank=True, validators=[MinValueValidator(50), MaxValueValidator(100)]
    )
    blood_sugar = models.PositiveSmallIntegerField(
        "blood sugar (mg/dL)", null=True, blank=True, validators=[MinValueValidator(20), MaxValueValidator(900)]
    )

    diagnosis = models.CharField(max_length=255, blank=True)
    plan = models.TextField("advice / plan", blank=True)
    follow_up_date = models.DateField(
        null=True, blank=True, help_text="When the patient should come back. Drives follow-up reminders."
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-visit_date"]
        indexes = [
            models.Index(fields=["clinic", "follow_up_date"]),
            models.Index(fields=["patient", "-visit_date"]),
        ]

    def __str__(self):
        return f"{self.patient.full_name} – {self.visit_date:%d %b %Y}"

    def get_absolute_url(self):
        return reverse("clinical:visit_detail", args=[self.pk])

    @property
    def blood_pressure(self):
        if self.bp_systolic and self.bp_diastolic:
            return f"{self.bp_systolic}/{self.bp_diastolic}"
        return ""

    @property
    def temperature_f(self):
        """The temperature in °F, rounded to 0.1 (it is stored in °C), or None."""
        if self.temperature_c is None:
            return None
        return round(float(self.temperature_c) * 9 / 5 + 32, 1)

    @property
    def bmi(self):
        if self.weight_kg and self.height_cm:
            metres = float(self.height_cm) / 100
            return round(float(self.weight_kg) / (metres * metres), 1)
        return None


class PrescriptionItem(models.Model):
    """One medicine line on a visit's prescription."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="prescription_items")
    medicine = models.CharField(max_length=200, help_text='e.g. "Tab. Amlodipine 5mg"')
    dose = models.CharField(max_length=100, blank=True, help_text='e.g. "1 tablet"')
    frequency = models.CharField(max_length=100, blank=True, help_text='e.g. "Twice daily (1+0+1)"')
    duration = models.CharField(max_length=100, blank=True, help_text='e.g. "5 days"')
    instructions = models.CharField(max_length=255, blank=True, help_text='e.g. "After meals"')
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.medicine


def lab_upload_path(instance, filename):
    """Random file names: no patient details ever appear in storage paths."""
    ext = os.path.splitext(filename)[1].lower()
    return f"clinic_{instance.clinic_id}/labs/{uuid.uuid4().hex}{ext}"


class LabResult(models.Model):
    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="lab_results")
    patient = models.ForeignKey("patients.Patient", on_delete=models.CASCADE, related_name="lab_results")
    visit = models.ForeignKey(Visit, on_delete=models.SET_NULL, null=True, blank=True, related_name="lab_results")

    test_name = models.CharField(max_length=200, help_text='e.g. "HbA1c", "CBC", "Chest X-ray"')
    result_date = models.DateField(default=timezone.localdate)
    result_text = models.TextField("result / values", blank=True)
    is_abnormal = models.BooleanField("abnormal result", default=False)
    file = models.FileField(
        upload_to=lab_upload_path,
        blank=True,
        validators=[FileExtensionValidator(settings.LAB_UPLOAD_EXTENSIONS)],
        help_text="PDF or photo of the report (optional)",
    )
    original_filename = models.CharField(max_length=255, blank=True, editable=False)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-result_date", "-created_at"]

    def __str__(self):
        return f"{self.test_name} – {self.result_date:%d %b %Y}"
