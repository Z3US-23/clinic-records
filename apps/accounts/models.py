from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models, transaction
from django.db.models import F
from django.utils.text import slugify


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """A person who signs in. Staff log in with their email address.

    `is_staff` / `is_superuser` only control the Django admin (the platform
    operator). What someone can do inside a clinic comes from `Membership.role`.
    """

    username = None
    first_name = None
    last_name = None
    email = models.EmailField("email address", unique=True)
    full_name = models.CharField(max_length=150)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name or self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name.split()[0] if self.full_name else self.email


class Clinic(models.Model):
    """A customer: one clinic. Every patient record belongs to exactly one clinic."""

    class Country(models.TextChoices):
        PAKISTAN = "PK", "Pakistan"
        INDIA = "IN", "India"

    TIMEZONE_CHOICES = [
        ("Asia/Karachi", "Pakistan time (PKT)"),
        ("Asia/Kolkata", "India time (IST)"),
    ]

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True)
    country = models.CharField(max_length=2, choices=Country.choices, default=Country.PAKISTAN)
    timezone = models.CharField(max_length=50, choices=TIMEZONE_CHOICES, default="Asia/Karachi")
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)

    # Reminder rules
    appointment_reminder_days = models.PositiveSmallIntegerField(
        default=1, help_text="Remind patients this many days before an appointment."
    )
    followup_reminder_days = models.PositiveSmallIntegerField(
        default=2, help_text="Remind patients this many days before a follow-up check-up is due."
    )
    overdue_grace_days = models.PositiveSmallIntegerField(
        default=3, help_text="Flag a follow-up as overdue this many days after it was due."
    )
    default_appointment_minutes = models.PositiveSmallIntegerField(default=15)

    # Printed prescription letterhead
    prescription_header = models.TextField(
        blank=True, help_text="Extra lines under the clinic name, e.g. clinic timings."
    )
    prescription_footer = models.TextField(blank=True)

    patient_counter = models.PositiveIntegerField(default=0, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:150] or "clinic"
            slug, n = base, 2
            while Clinic.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def allocate_mrn(self):
        """Next medical record number for this clinic, e.g. 'P-00042'. Safe under concurrency."""
        with transaction.atomic():
            Clinic.objects.filter(pk=self.pk).update(patient_counter=F("patient_counter") + 1)
            self.refresh_from_db(fields=["patient_counter"])
        return f"P-{self.patient_counter:05d}"

    @property
    def doctors(self):
        """Users who can be booked / write clinical notes in this clinic."""
        return User.objects.filter(
            memberships__clinic=self,
            memberships__is_active=True,
            memberships__role__in=Membership.CLINICAL_ROLES,
        ).distinct()


class Membership(models.Model):
    """Links a user to a clinic with a role. A doctor may work at more than one clinic."""

    class Role(models.TextChoices):
        OWNER = "owner", "Clinic owner"
        DOCTOR = "doctor", "Doctor"
        RECEPTIONIST = "receptionist", "Receptionist"

    # Roles that may see clinical notes, prescriptions and lab results.
    CLINICAL_ROLES = (Role.OWNER, Role.DOCTOR)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECEPTIONIST)

    # Shown on printed prescriptions for doctors.
    title = models.CharField(max_length=20, blank=True, default="", help_text='e.g. "Dr."')
    qualifications = models.CharField(max_length=150, blank=True, help_text="e.g. MBBS, FCPS (Medicine)")
    registration_number = models.CharField(
        max_length=50, blank=True, help_text="PMDC / NMC registration number"
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["clinic", "user__full_name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "clinic"], name="unique_membership_per_clinic"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.clinic} ({self.get_role_display()})"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    @property
    def is_clinician(self):
        return self.role in self.CLINICAL_ROLES

    @property
    def display_name(self):
        return f"{self.title} {self.user.full_name}".strip()
