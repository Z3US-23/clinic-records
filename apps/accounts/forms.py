"""Forms for signing in, registering a clinic, managing staff and clinic settings."""

from django import forms
from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.audit import get_client_ip
from apps.core.phone import normalize_phone

from . import lockout
from .models import Clinic, Membership, User

Role = Membership.Role

# Each country's clinics run on its own clock.
COUNTRY_TIMEZONES = {
    Clinic.Country.PAKISTAN: "Asia/Karachi",
    Clinic.Country.INDIA: "Asia/Kolkata",
}

PHONE_ERROR = "Enter a phone number we can understand, e.g. 0300-1234567 or 98765 43210."


def password_help_text():
    """A short, plain-English summary of the password rules in settings."""
    min_length = 8
    for validator in password_validation.get_default_password_validators():
        min_length = getattr(validator, "min_length", min_length)
    return f"At least {min_length} characters. Avoid common words, names and number-only passwords."


def new_password_fields(required=True):
    """Two password inputs: the new password and the same again to confirm."""
    password1 = forms.CharField(
        label="Password",
        required=required,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        required=required,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Type the same password again.",
    )
    return password1, password2


def check_new_password(form, user):
    """Check password1/password2 match and pass Django's password rules for `user`."""
    password1 = form.cleaned_data.get("password1")
    password2 = form.cleaned_data.get("password2")
    if not password1:
        return
    if password1 != password2:
        form.add_error("password2", "The two passwords don't match.")
        return
    try:
        password_validation.validate_password(password1, user)
    except ValidationError as error:
        form.add_error("password1", error)


def check_phone(form, field_name, country):
    phone = form.cleaned_data.get(field_name)
    if phone and not normalize_phone(phone, country or Clinic.Country.PAKISTAN):
        form.add_error(field_name, PHONE_ERROR)


# --- Signing in ---------------------------------------------------------------


def locked_error(minutes):
    """The error shown while sign-ins for an email/address are locked (see lockout.py)."""
    return ValidationError(
        "Too many failed attempts. Please wait %(minutes)s and try again.",
        code="locked",
        params={"minutes": f"{minutes} minute{'s' if minutes != 1 else ''}"},
    )


class LoginLockoutMixin:
    """Refuse sign-in after too many wrong passwords (see apps.accounts.lockout).

    Put it BEFORE the Django form class (class MyForm(LoginLockoutMixin, AuthenticationForm))
    so that its clean() wraps the password check. Both sign-in pages use it and share the
    same counters, so failures on either page count towards the same lock.
    """

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()

    def clean(self):
        email = self.cleaned_data.get("username")
        ip = get_client_ip(self.request)

        if email:
            minutes = lockout.minutes_locked(email, ip)
            if minutes:
                # Locked: refuse without checking the password at all.
                raise locked_error(minutes)

        try:
            cleaned_data = super().clean()  # calls authenticate()
        except ValidationError:
            if email and self.cleaned_data.get("password"):
                lockout.record_failure(email, ip)
            raise

        if email and self.user_cache is not None:
            lockout.clear(email, ip)
        return cleaned_data


class EmailAuthenticationForm(LoginLockoutMixin, AuthenticationForm):
    """The app's sign-in page: email + password."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    error_messages = {
        "invalid_login": "That email and password don't match. Please check them and try again.",
        "inactive": "This account has been switched off. Please contact your clinic owner.",
    }


class LockoutAdminAuthenticationForm(LoginLockoutMixin, AdminAuthenticationForm):
    """The Django admin's sign-in page, with the same lockout as the app's own sign-in page.

    Platform admins can read every clinic's records, so their password needs this most.
    """


class ChangePasswordForm(PasswordChangeForm):
    """Django's change-password form with friendlier labels."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Current password"
        self.fields["new_password1"].help_text = password_help_text()
        self.fields["new_password2"].label = "Confirm new password"
        self.fields["new_password2"].help_text = "Type the new password again."


class StaffSetPasswordForm(SetPasswordForm):
    """An owner sets a new password for a staff member."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["new_password1"].help_text = password_help_text()
        self.fields["new_password1"].widget.attrs["autofocus"] = True
        self.fields["new_password2"].label = "Confirm new password"
        self.fields["new_password2"].help_text = "Type the new password again."


# --- Registering a new clinic -----------------------------------------------


class ClinicSignupForm(forms.Form):
    """One page that creates a clinic and its owner's account."""

    clinic_name = forms.CharField(label="Clinic name", max_length=150)
    country = forms.ChoiceField(choices=Clinic.Country.choices, initial=Clinic.Country.PAKISTAN)
    city = forms.CharField(max_length=100, required=False)
    clinic_phone = forms.CharField(
        label="Clinic phone",
        max_length=30,
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
        help_text="Patients see this number in reminder messages.",
    )
    full_name = forms.CharField(
        label="Your full name", max_length=150, widget=forms.TextInput(attrs={"autocomplete": "name"})
    )
    title = forms.CharField(
        max_length=20, required=False, initial="Dr.", help_text='Printed before your name, e.g. "Dr.".'
    )
    email = forms.EmailField(
        label="Your email",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
        help_text="You will sign in with this email.",
    )
    password1, password2 = new_password_fields()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].help_text = password_help_text()

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists. Please sign in instead.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        check_phone(self, "clinic_phone", cleaned_data.get("country"))
        check_new_password(
            self, User(email=cleaned_data.get("email", ""), full_name=cleaned_data.get("full_name", ""))
        )
        return cleaned_data

    def save(self):
        """Create the clinic, the owner's account and their membership together."""
        data = self.cleaned_data
        with transaction.atomic():
            clinic = Clinic.objects.create(
                name=data["clinic_name"],
                country=data["country"],
                timezone=COUNTRY_TIMEZONES.get(data["country"], "Asia/Karachi"),
                city=data["city"],
                phone=data["clinic_phone"],
            )
            user = User.objects.create_user(
                email=data["email"], password=data["password1"], full_name=data["full_name"]
            )
            Membership.objects.create(user=user, clinic=clinic, role=Role.OWNER, title=data["title"])
        return clinic, user


# --- Your own profile -------------------------------------------------------


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name"]
        labels = {"full_name": "Full name"}


class PrescriptionDetailsForm(forms.ModelForm):
    """What a doctor's name looks like on printed prescriptions (per clinic)."""

    class Meta:
        model = Membership
        fields = ["title", "qualifications", "registration_number"]
        labels = {"registration_number": "Registration number"}
        help_texts = {"registration_number": "PMDC (Pakistan) or NMC / state council (India) number."}


# --- Staff ----------------------------------------------------------------------

STAFF_LABELS = {
    "role": "Role",
    "title": "Title",
    "qualifications": "Qualifications",
    "registration_number": "Registration number",
    "is_active": "Can use this clinic's account",
}

STAFF_HELP = {
    "title": 'Printed before the name, e.g. "Dr.". Leave blank for receptionists.',
    "qualifications": "Doctors only, e.g. MBBS, FCPS (Medicine).",
    "registration_number": "Doctors only: PMDC or NMC registration number, printed on prescriptions.",
    "is_active": "Untick to switch off their access. Their past records stay as they are.",
}


class StaffAddForm(forms.Form):
    """Add a person to the clinic.

    * A new email gets a new account with the password typed here. The owner knows that
      password, so the person must choose their own when they first sign in.
    * An email that already has an account (they work at another clinic) gets an
      invitation instead: they join only after accepting it themselves (see Membership).
      Nothing about the existing account (its name, its password) is shown or changed.
    """

    full_name = forms.CharField(label="Full name", max_length=150)
    email = forms.EmailField(help_text="They will sign in with this email.")
    role = forms.ChoiceField(choices=Role.choices, initial=Role.RECEPTIONIST)
    title = forms.CharField(max_length=20, required=False, help_text=STAFF_HELP["title"])
    qualifications = forms.CharField(max_length=150, required=False, help_text=STAFF_HELP["qualifications"])
    registration_number = forms.CharField(
        label="Registration number", max_length=50, required=False, help_text=STAFF_HELP["registration_number"]
    )
    password1, password2 = new_password_fields(required=False)

    def __init__(self, *args, clinic, **kwargs):
        self.clinic = clinic
        self.existing_user = None
        super().__init__(*args, **kwargs)
        self.fields["password1"].help_text = (
            password_help_text() + f" Not needed if they already use {settings.PRODUCT_NAME} at another clinic."
        )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        self.existing_user = User.objects.filter(email__iexact=email).first()
        if self.existing_user is not None:
            membership = Membership.objects.filter(user=self.existing_user, clinic=self.clinic).first()
            if membership is None:
                pass  # they will get an invitation
            elif membership.is_pending:
                # Not a member yet: name them only by the email the owner typed.
                raise ValidationError(
                    f"You have already invited {email}. Open their row on the staff list to see the join link."
                )
            elif membership.is_active:
                raise ValidationError(f"{self.existing_user} is already on your staff list.")
            else:
                raise ValidationError(
                    f"{self.existing_user} is already on your staff list but switched off. "
                    "Open their details from the staff list to switch them back on."
                )
        return email

    def clean(self):
        cleaned_data = super().clean()
        if self.existing_user is None and cleaned_data.get("email"):
            # A brand-new account needs a password for its first sign-in.
            if not cleaned_data.get("password1"):
                self.add_error("password1", "Choose a password for their first sign-in.")
            else:
                check_new_password(
                    self, User(email=cleaned_data["email"], full_name=cleaned_data.get("full_name", ""))
                )
        return cleaned_data

    def save(self):
        """Returns (membership, created_new_account).

        With an existing account the membership is a waiting invitation (no access yet).
        """
        data = self.cleaned_data
        with transaction.atomic():
            user = self.existing_user
            created = user is None
            if created:
                user = User.objects.create_user(
                    email=data["email"],
                    password=data["password1"],
                    full_name=data["full_name"],
                    must_change_password=True,  # the owner knows this password
                )
            membership = Membership(
                user=user,
                clinic=self.clinic,
                role=data["role"],
                title=data["title"],
                qualifications=data["qualifications"],
                registration_number=data["registration_number"],
            )
            if created:
                membership.save()
            else:
                # An existing account keeps its own name and password, and joins only by accepting.
                membership.start_invitation()
        return membership, created


class StaffEditForm(forms.ModelForm):
    """Change a staff member's role, printed details or access."""

    class Meta:
        model = Membership
        fields = ["role", "title", "qualifications", "registration_number", "is_active"]
        labels = STAFF_LABELS
        help_texts = STAFF_HELP

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.is_pending:
            # Access starts only when they accept the invitation themselves.
            del self.fields["is_active"]

    def clean(self):
        cleaned_data = super().clean()
        if "role" not in cleaned_data:
            return cleaned_data

        # self.instance still holds the saved values here (the form copies
        # cleaned data onto it only after clean()).
        was_active_owner = self.instance.is_active and self.instance.role == Role.OWNER
        stays_active_owner = cleaned_data.get("is_active") and cleaned_data["role"] == Role.OWNER
        if was_active_owner and not stays_active_owner:
            other_owners = Membership.objects.filter(
                clinic=self.instance.clinic, role=Role.OWNER, is_active=True
            ).exclude(pk=self.instance.pk)
            if not other_owners.exists():
                raise ValidationError(
                    f"{self.instance.user} is the clinic's only active owner. "
                    "Make another staff member an owner first, so someone can still manage staff and settings."
                )
        return cleaned_data


# --- Joining another clinic (accepting an invitation) -----------------------


class AcceptInvitationForm(forms.Form):
    """Accepting an invitation opens another clinic's records, so confirm it's really you.

    Wrong passwords count towards the sign-in lockout, so this page can't be used to guess one.
    """

    password = forms.CharField(
        label="Your password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "autofocus": True}),
        help_text="The password you already use to sign in.",
    )

    def __init__(self, *args, request, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)

    def clean_password(self):
        user = self.request.user
        ip = get_client_ip(self.request)
        minutes = lockout.minutes_locked(user.email, ip)
        if minutes:
            raise locked_error(minutes)

        password = self.cleaned_data["password"]
        if not user.check_password(password):
            lockout.record_failure(user.email, ip)
            raise ValidationError("That isn't your password. Please try again.", code="wrong_password")
        return password


# --- Clinic settings --------------------------------------------------------


class ClinicSettingsForm(forms.ModelForm):
    appointment_reminder_days = forms.IntegerField(
        label="Appointment reminder (days before)",
        min_value=0,
        max_value=14,
        help_text="1 = the day before the appointment. 0 = on the same day.",
    )
    followup_reminder_days = forms.IntegerField(
        label="Follow-up reminder (days before)",
        min_value=0,
        max_value=14,
        help_text="How early to remind patients that a follow-up check-up is due.",
    )
    overdue_grace_days = forms.IntegerField(
        label="Missed follow-up (days after)",
        min_value=1,
        max_value=60,
        help_text=(
            "Prepare a 'please visit us' message this many days after the due date "
            "if the patient hasn't come back (1 = the day after)."
        ),
    )
    default_appointment_minutes = forms.IntegerField(
        label="Usual appointment length (minutes)",
        min_value=5,
        max_value=120,
        help_text="Used to fill in the length when booking an appointment.",
    )

    class Meta:
        model = Clinic
        fields = [
            "name",
            "address",
            "city",
            "phone",
            "email",
            "country",
            "timezone",
            "appointment_reminder_days",
            "followup_reminder_days",
            "overdue_grace_days",
            "default_appointment_minutes",
            "prescription_header",
            "prescription_footer",
            "prescription_paper",
            "prescription_pad_space",
        ]
        labels = {
            "name": "Clinic name",
            "phone": "Clinic phone",
            "email": "Clinic email",
            "timezone": "Time zone",
            "prescription_header": "Letterhead lines",
            "prescription_footer": "Footer",
            "prescription_paper": "Paper size",
            "prescription_pad_space": "Pre-printed pad",
        }
        help_texts = {
            "phone": "Shown in reminder messages and on printed prescriptions.",
            "timezone": "Appointment times and reminders follow this clock.",
            "prescription_header": "Extra lines under the clinic name, e.g. clinic timings or a second phone number.",
            "prescription_footer": "Printed at the bottom, e.g. \"Please bring this prescription on your next visit.\"",
            "prescription_paper": "The usual paper for printed prescriptions. Doctors can switch on the print page.",
            "prescription_pad_space": (
                "For pads that already have your letterhead printed on them: the app leaves this much "
                "space blank at the top instead of printing its own letterhead."
            ),
        }
        widgets = {
            "phone": forms.TextInput(attrs={"type": "tel"}),
            "prescription_header": forms.Textarea(attrs={"rows": 3}),
            "prescription_footer": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        cleaned_data = super().clean()
        check_phone(self, "phone", cleaned_data.get("country"))
        return cleaned_data
