import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone
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
    # A clinic owner chose this password (new staff account, or a reset). Someone else
    # knows it, so the person must pick their own before using the app
    # (see apps.accounts.middleware.PasswordChangeRequiredMiddleware).
    must_change_password = models.BooleanField(
        default=False,
        help_text="Set when a clinic owner chose the password. Cleared when they change it themselves.",
    )

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

    # Printed prescriptions (apps.clinical.utils.prescription_print_options reads these as the
    # clinic's defaults; the print page can still switch for one print).
    PAPER_CHOICES = [("a5", "A5 (half sheet)"), ("a4", "A4 (full sheet)")]
    PAD_SPACE_CHOICES = [
        (0, "No: print our letterhead"),
        (30, "Yes: leave 3 cm blank"),
        (40, "Yes: leave 4 cm blank"),
        (50, "Yes: leave 5 cm blank"),
        (60, "Yes: leave 6 cm blank"),
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
        default=3,
        validators=[MinValueValidator(1)],
        help_text="Flag a follow-up as overdue this many days after it was due (1 = the day after).",
    )
    default_appointment_minutes = models.PositiveSmallIntegerField(default=15)

    # Printed prescription letterhead
    prescription_header = models.TextField(
        blank=True, help_text="Extra lines under the clinic name, e.g. clinic timings."
    )
    prescription_footer = models.TextField(blank=True)
    prescription_paper = models.CharField(max_length=2, choices=PAPER_CHOICES, default="a5")
    prescription_pad_space = models.PositiveSmallIntegerField(
        choices=PAD_SPACE_CHOICES,
        default=0,
        help_text="For pads that already have your letterhead printed on them.",
    )

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

    @staticmethod
    def format_mrn(number):
        """The automatic MR number for a counter value: 42 -> 'P-00042'."""
        return f"P-{number:05d}"

    def allocate_mrn(self):
        """Next medical record number for this clinic, e.g. 'P-00042'. Safe under concurrency."""
        with transaction.atomic():
            Clinic.objects.filter(pk=self.pk).update(patient_counter=F("patient_counter") + 1)
            self.refresh_from_db(fields=["patient_counter"])
        return self.format_mrn(self.patient_counter)

    @property
    def doctors(self):
        """Users who can be booked / write clinical notes in this clinic.

        Only active memberships count, and a waiting invitation is never active.
        """
        return User.objects.filter(
            memberships__clinic=self,
            memberships__is_active=True,
            memberships__accepted_at__isnull=False,
            memberships__role__in=Membership.CLINICAL_ROLES,
        ).distinct()


class Membership(models.Model):
    """Links a user to a clinic with a role. A doctor may work at more than one clinic.

    Invitations: when an owner adds someone who ALREADY has an account (they work at
    another clinic), that person does not get access straight away. The membership
    waits (`accepted_at` empty, always `is_active=False`) until they sign in, open the
    join link the owner gave them and accept. Knowing their password is not enough to
    accept, so one clinic can never add itself to an account another clinic controls.
    Until then this clinic only sees the email it typed, not the account's name.
    """

    INVITE_VALID_DAYS = 7

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

    # Invitations (see the class docstring). Most memberships are accepted the moment they
    # are made (a new staff account, the owner at sign-up), hence the default.
    accepted_at = models.DateTimeField(
        null=True, blank=True, default=timezone.now, help_text="Empty while an invitation is waiting."
    )
    invited_at = models.DateTimeField(null=True, blank=True)
    invite_token = models.CharField(max_length=64, blank=True, db_index=True, editable=False)

    class Meta:
        ordering = ["clinic", "user__full_name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "clinic"], name="unique_membership_per_clinic"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.clinic} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        if self.accepted_at is None:
            # An invitation nobody has accepted yet never gives access.
            self.is_active = False
        super().save(*args, **kwargs)

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    @property
    def is_clinician(self):
        return self.role in self.CLINICAL_ROLES

    @property
    def display_name(self):
        """How this clinic names the person, e.g. "Dr. Sara Ahmed".

        While an invitation is waiting it is just the email the owner typed: the
        account and its name belong to the person, not to the clinic that invited them.
        """
        if self.is_pending:
            return self.user.email
        return f"{self.title} {self.user.full_name}".strip()

    # --- Invitations ---------------------------------------------------------------

    @property
    def is_pending(self):
        """True while an invitation is waiting for the person to accept it."""
        return self.accepted_at is None

    @property
    def invite_expires_at(self):
        if self.invited_at is None:
            return None
        return self.invited_at + timedelta(days=self.INVITE_VALID_DAYS)

    @property
    def invite_expired(self):
        return self.invited_at is None or timezone.now() >= self.invite_expires_at

    def get_invite_url(self):
        """Absolute join link the owner sends to the person they invited."""
        return settings.SITE_URL + reverse("accounts:join", args=[self.invite_token])

    def start_invitation(self):
        """Save this membership as a waiting invitation with a fresh join link.

        Also used to make a new link: the old one stops working.
        """
        self.accepted_at = None
        self.is_active = False
        self.invited_at = timezone.now()
        self.invite_token = secrets.token_urlsafe(32)
        self.save()

    def accept_invitation(self):
        """The invited person accepted: they can work in this clinic from now on."""
        self.accepted_at = timezone.now()
        self.is_active = True
        self.invite_token = ""
        self.save(update_fields=["accepted_at", "is_active", "invite_token"])
