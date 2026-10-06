from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from apps.core.phone import normalize_phone


class Patient(models.Model):
    class Sex(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"
        OTHER = "O", "Other"

    class BloodGroup(models.TextChoices):
        A_POS = "A+", "A+"
        A_NEG = "A-", "A-"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B-"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB-"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O-"

    clinic = models.ForeignKey("accounts.Clinic", on_delete=models.CASCADE, related_name="patients")
    mrn = models.CharField("MR number", max_length=20, help_text="Medical record number, unique within the clinic")

    full_name = models.CharField(max_length=150)
    guardian_name = models.CharField("father / husband name", max_length=150, blank=True)
    sex = models.CharField(max_length=1, choices=Sex.choices)
    date_of_birth = models.DateField(null=True, blank=True)
    dob_is_estimated = models.BooleanField(
        default=False, help_text="Date of birth was worked out from an age the patient gave."
    )

    phone = models.CharField(max_length=30, help_text="Mobile number")
    whatsapp_phone = models.CharField(
        "WhatsApp number", max_length=30, blank=True, help_text="Only if different from the mobile number"
    )
    # Digits-only international form of the WhatsApp number (e.g. 923001234567), set on save.
    whatsapp_number = models.CharField(max_length=20, blank=True, editable=False)
    reminders_opt_in = models.BooleanField(
        "agrees to WhatsApp reminders", default=True,
        help_text="Patient agreed to receive appointment and follow-up reminders.",
    )

    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)

    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)
    allergies = models.TextField(blank=True, help_text="Drug or food allergies. Shown in red on every screen.")
    chronic_conditions = models.TextField(blank=True, help_text="e.g. Diabetes type 2, Hypertension")
    notes = models.TextField("front-desk notes", blank=True, help_text="Non-clinical notes, e.g. preferred language")

    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(fields=["clinic", "mrn"], name="unique_mrn_per_clinic"),
        ]
        indexes = [
            models.Index(fields=["clinic", "full_name"]),
            models.Index(fields=["clinic", "phone"]),
        ]

    def __str__(self):
        return f"{self.full_name} ({self.mrn})"

    def save(self, *args, **kwargs):
        if not self.mrn:
            self.mrn = self.clinic.allocate_mrn()
        self.whatsapp_number = normalize_phone(self.whatsapp_phone or self.phone, self.clinic.country)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("patients:detail", args=[self.pk])

    @property
    def first_name(self):
        return self.full_name.split()[0] if self.full_name else ""

    @property
    def age(self):
        """Age in whole years, or None if no date of birth."""
        if not self.date_of_birth:
            return None
        today = timezone.localdate()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    @property
    def age_display(self):
        """Short age label: '34 y', '8 mo' for infants, '' if unknown."""
        if not self.date_of_birth:
            return ""
        years = self.age
        if years >= 2:
            return f"{years} y"
        today = timezone.localdate()
        months = (today.year - self.date_of_birth.year) * 12 + today.month - self.date_of_birth.month
        if today.day < self.date_of_birth.day:
            months -= 1
        return f"{max(months, 0)} mo"

    @property
    def can_receive_whatsapp(self):
        return bool(self.whatsapp_number) and self.reminders_opt_in and not self.is_archived
