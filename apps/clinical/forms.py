from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import formats, timezone

from apps.accounts.models import User

from .models import LabResult, PrescriptionItem, Visit
from .uploads import allowed_extensions, validate_lab_file
from .utils import FOLLOW_UP_CHOICES, follow_up_from

# HTML date inputs always send and expect YYYY-MM-DD, whatever the site's language.
ISO_DATE = "%Y-%m-%d"

VITAL_FIELDS = [
    "bp_systolic",
    "bp_diastolic",
    "pulse",
    "temperature_c",
    "spo2",
    "weight_kg",
    "height_cm",
    "blood_sugar",
]

# Form prefix of the prescription rows (inputs are named items-0-medicine, items-1-medicine, ...).
PRESCRIPTION_PREFIX = "items"
MAX_MEDICINES = 40

# Temperature is stored in °C (the model allows 30–45 °C), but most doctors here measure in °F.
# The form takes either: 30–45 °C is 86–113 °F, so a number can only be in one of the two ranges.
CELSIUS_RANGE = (30, 45)
FAHRENHEIT_RANGE = (86, 113)


def date_input(**attrs):
    return forms.DateInput(attrs={"type": "date", **attrs}, format=ISO_DATE)


# --- Visit ------------------------------------------------------------------------

class VisitForm(forms.ModelForm):
    """Consultation notes, vitals, diagnosis and follow-up. Prescription rows are a separate formset."""

    # The day part of Visit.visit_date: today for a new visit, or an earlier day when a visit is
    # typed in from a paper file. save() turns it back into a date and time.
    seen_on = forms.DateField(
        label="Visit date",
        required=False,
        widget=date_input(**{"data-visit-date": True}),
        help_text="Change only when entering an earlier visit, e.g. from a paper file.",
    )
    # Stored in °C, typed in °F or °C: see clean_temperature_c(). Two decimals so "98.65" is accepted.
    temperature_c = forms.DecimalField(
        label="Temperature (°F or °C)",
        required=False,
        max_digits=5,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"placeholder": "e.g. 101 or 38.3"}),
    )
    # Not stored: a no-JavaScript way to set the follow-up date ("come back in 2 weeks").
    # With JavaScript, clinical.js fills the date box as soon as one is picked.
    follow_up_in = forms.ChoiceField(
        label="Or come back in",
        choices=FOLLOW_UP_CHOICES,
        required=False,
        widget=forms.RadioSelect,
    )

    class Meta:
        model = Visit
        fields = ["chief_complaint", "history", "examination", *VITAL_FIELDS, "diagnosis", "plan", "follow_up_date"]
        labels = {
            "chief_complaint": "Presenting complaint",
            "history": "History",
            "examination": "Examination findings",
            "diagnosis": "Diagnosis",
            "plan": "Advice / plan",
            "follow_up_date": "Follow-up date",
        }
        help_texts = {
            "follow_up_date": "When the patient should come back. A WhatsApp reminder is prepared before this date.",
        }
        widgets = {
            "chief_complaint": forms.TextInput(attrs={"placeholder": "e.g. Fever and cough for 3 days"}),
            "history": forms.Textarea(attrs={"rows": 3}),
            "examination": forms.Textarea(attrs={"rows": 3}),
            "diagnosis": forms.TextInput(attrs={"placeholder": "e.g. Acute bronchitis"}),
            "plan": forms.Textarea(attrs={"rows": 3, "placeholder": "Advice for the patient, tests to do, etc."}),
            "follow_up_date": date_input(),
            "bp_systolic": forms.NumberInput(attrs={"placeholder": "120", "aria-label": "BP systolic (top number)"}),
            "bp_diastolic": forms.NumberInput(attrs={"placeholder": "80", "aria-label": "BP diastolic (bottom number)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Tell the browser the allowed range of each vital too, so typos are caught before saving.
        # (The model's validators still check on the server.)
        for name in VITAL_FIELDS:
            field = self.fields[name]
            for validator in Visit._meta.get_field(name).validators:
                if isinstance(validator, MinValueValidator):
                    field.widget.attrs["min"] = validator.limit_value
                elif isinstance(validator, MaxValueValidator):
                    field.widget.attrs["max"] = validator.limit_value
            field.widget.attrs["inputmode"] = "decimal" if isinstance(field, forms.DecimalField) else "numeric"
        # The temperature box takes °F as well, so the browser must allow up to 113.
        self.fields["temperature_c"].widget.attrs["max"] = FAHRENHEIT_RANGE[1]

        seen_on = self.fields["seen_on"]
        seen_on.initial = self.saved_day
        seen_on.widget.attrs["max"] = timezone.localdate().isoformat()

    @property
    def saved_day(self):
        """The visit's day as saved (today for a new visit), in the clinic's timezone."""
        return timezone.localdate(self.instance.visit_date)

    @property
    def visit_day(self):
        """The visit's day: the "Visit date" box once the form is checked, otherwise the saved day.

        Follow-up quick picks count from here, and the follow-up can't be before it.
        """
        chosen = getattr(self, "cleaned_data", {}).get("seen_on")
        return chosen or self.saved_day

    @property
    def is_back_dated(self):
        """True when the visit is on an earlier day than today (e.g. typed in from a paper file)."""
        return self.visit_day < timezone.localdate()

    def clean_seen_on(self):
        day = self.cleaned_data.get("seen_on")
        if day is None:
            return self.saved_day  # left empty: the visit keeps its day
        if day > timezone.localdate():
            raise forms.ValidationError("The visit date can't be in the future.")
        born = self.instance.patient.date_of_birth if self.instance.patient_id else None
        if born and day < born:
            raise forms.ValidationError("The visit date can't be before the patient's date of birth.")
        return day

    def clean_temperature_c(self):
        """Accept °F or °C and return °C (the unit the visit stores), rounded like the model (0.1)."""
        value = self.cleaned_data.get("temperature_c")
        if value is None:
            return None
        if CELSIUS_RANGE[0] <= value <= CELSIUS_RANGE[1]:
            celsius = value
        elif FAHRENHEIT_RANGE[0] <= value <= FAHRENHEIT_RANGE[1]:
            celsius = (value - 32) * 5 / 9
        else:
            raise forms.ValidationError("Enter the temperature in °F (e.g. 101.2) or °C (e.g. 38.4).")
        return celsius.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    def clean(self):
        cleaned = super().clean()

        systolic = cleaned.get("bp_systolic")
        diastolic = cleaned.get("bp_diastolic")
        bp_has_errors = self.has_error("bp_systolic") or self.has_error("bp_diastolic")
        if not bp_has_errors and (systolic is None) != (diastolic is None):
            missing = "bp_diastolic" if diastolic is None else "bp_systolic"
            self.add_error(missing, "Enter both blood pressure numbers (e.g. 120 / 80).")
        elif systolic is not None and diastolic is not None and systolic <= diastolic:
            self.add_error("bp_systolic", "The top number should be higher than the bottom number.")

        # A quick pick ("2 weeks") is a deliberate choice, so it wins over the date box.
        # With JavaScript both always agree: picking fills the box, typing a date clears the pick.
        follow_up = cleaned.get("follow_up_date")
        quick_pick = cleaned.get("follow_up_in")
        if quick_pick and not self.has_error("follow_up_date"):
            follow_up = follow_up_from(self.visit_day, quick_pick)
            cleaned["follow_up_date"] = follow_up
        if follow_up and follow_up < self.visit_day:
            self.add_error("follow_up_date", "The follow-up date can't be before the visit.")
        return cleaned

    def chosen_visit_datetime(self):
        """Visit.visit_date with the "Visit date" box applied (call on a valid form).

        Same day as saved: unchanged, so a new visit keeps "now" and an edit keeps its time.
        Another day: that day at the same time of day, in the clinic's timezone. (The real time of
        a visit typed in from paper isn't known; keeping one keeps visits in a stable order.)
        """
        day = self.cleaned_data.get("seen_on") or self.saved_day
        if day == self.saved_day:
            return self.instance.visit_date
        time_of_day = timezone.localtime(self.instance.visit_date).time()
        return timezone.make_aware(datetime.combine(day, time_of_day))

    def save(self, commit=True):
        self.instance.visit_date = self.chosen_visit_datetime()
        return super().save(commit)


class PrescriptionItemForm(forms.ModelForm):
    class Meta:
        model = PrescriptionItem
        fields = ["medicine", "dose", "frequency", "duration", "instructions"]
        labels = {
            "medicine": "Medicine",
            "dose": "Dose",
            "frequency": "How often",
            "duration": "For how long",
            "instructions": "Instructions",
        }
        widgets = {
            "medicine": forms.TextInput(
                attrs={"placeholder": "e.g. Tab. Amlodipine 5mg", "list": "medicine-options", "autocomplete": "off"}
            ),
            "dose": forms.TextInput(attrs={"placeholder": "1 tablet"}),
            "frequency": forms.TextInput(attrs={"placeholder": "1+0+1"}),
            "duration": forms.TextInput(attrs={"placeholder": "5 days"}),
            "instructions": forms.TextInput(attrs={"placeholder": "After meals"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A row without a medicine is simply ignored (see PrescriptionFormSet.save_items),
        # so a half-filled spare row never blocks saving the visit.
        self.fields["medicine"].required = False
        for field in self.fields.values():
            field.help_text = ""


class PrescriptionFormSet(BaseInlineFormSet):
    """The medicine rows under a visit."""

    default_error_messages = {
        "too_many_forms": "A prescription can have at most %(num)d medicines.",
    }

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        if self._is_stale(i, form):
            # The row's medicine has been removed from the visit since the page was opened
            # (in another tab, or Save was pressed twice), so its hidden id no longer exists.
            # Django would reject the row with an error on that hidden field, which nobody can
            # see or fix, and every new try would fail the same way. Accept the row instead:
            # save_items() skips it. (Turning it into a new row would bring back a medicine
            # that someone deliberately removed.)
            form.fields[self.model._meta.pk.name] = forms.Field(required=False, widget=forms.HiddenInput)
        return form

    def _is_stale(self, index, form):
        """An "existing" row whose medicine isn't on this visit (removed meanwhile, or tampered POST data)."""
        return self.is_bound and index < self.initial_form_count() and form.instance.pk is None

    @property
    def stale_medicines(self):
        """How many skipped stale rows still had a medicine on them (call after is_valid()).

        The doctor should hear about these: a change typed into such a row is not saved.
        """
        count = 0
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if self._is_stale(index, form) and (data.get("medicine") or "").strip() and not data.get("DELETE"):
                count += 1
        return count

    def save_items(self, visit):
        """Save rows in on-screen order and return how many medicines were kept.

        * Rows without a medicine are ignored (an existing row that was emptied is removed).
        * Rows ticked "Remove" are deleted.
        * `order` follows the row's position on the page.
        * An "existing" row that isn't on this visit (removed meanwhile, or tampered
          POST data) is skipped, as Django's own formset.save() does.
        """
        position = 0
        for index, form in enumerate(self.forms):
            if self._is_stale(index, form):
                continue
            data = getattr(form, "cleaned_data", {})
            medicine = (data.get("medicine") or "").strip()
            remove = self.can_delete and data.get("DELETE", False)
            if remove or not medicine:
                if form.instance.pk:
                    form.instance.delete()
                continue
            item = form.save(commit=False)
            item.visit = visit
            item.medicine = medicine
            item.order = position
            item.save()
            position += 1
        return position


def prescription_formset_class(*, editing, extra=None):
    """Formset class for the prescription rows: 3 spare rows for a new visit, 1 when editing.

    Removing rows is only offered when editing (a new visit's rows can simply be left empty).
    """
    if extra is None:
        extra = 1 if editing else 3
    return inlineformset_factory(
        Visit,
        PrescriptionItem,
        form=PrescriptionItemForm,
        formset=PrescriptionFormSet,
        extra=extra,
        can_delete=editing,
        can_delete_extra=False,
        max_num=MAX_MEDICINES,
        validate_max=True,
    )


# --- Visit list filters -------------------------------------------------------------

class VisitFilterForm(forms.Form):
    """Filters on the visits page. Invalid values are ignored instead of causing an error."""

    q = forms.CharField(
        label="Patient",
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": "Patient name or MR no.", "aria-label": "Patient name or MR number"}
        ),
    )
    doctor = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="All doctors",
        widget=forms.Select(attrs={"aria-label": "Doctor"}),
    )
    date_from = forms.DateField(label="From", required=False, widget=date_input())
    date_to = forms.DateField(label="To", required=False, widget=date_input())

    def __init__(self, *args, clinic, **kwargs):
        super().__init__(*args, **kwargs)
        # The clinic's current doctors, plus anyone who has recorded visits here before.
        self.fields["doctor"].queryset = (
            User.objects.filter(Q(pk__in=clinic.doctors.values("pk")) | Q(visits__clinic=clinic))
            .distinct()
            .order_by("full_name")
        )

    def filter(self, visits):
        """Apply every filter that has a valid value."""
        if not self.is_bound:
            return visits
        self.is_valid()  # fills cleaned_data with the valid fields only
        data = self.cleaned_data
        if data.get("q"):
            q = data["q"].strip()
            visits = visits.filter(Q(patient__full_name__icontains=q) | Q(patient__mrn__iexact=q))
        if data.get("doctor"):
            visits = visits.filter(doctor=data["doctor"])
        if data.get("date_from"):
            visits = visits.filter(visit_date__date__gte=data["date_from"])
        if data.get("date_to"):
            visits = visits.filter(visit_date__date__lte=data["date_to"])
        return visits

    @property
    def is_filtered(self):
        return self.is_bound and any(self.data.get(name) for name in self.fields)


# --- Lab results --------------------------------------------------------------------

class VisitChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, visit):
        when = formats.date_format(timezone.localtime(visit.visit_date), "j M Y")
        return f"{when} – {visit.chief_complaint}"


class LabResultForm(forms.ModelForm):
    class Meta:
        model = LabResult
        fields = ["test_name", "result_date", "visit", "is_abnormal", "result_text", "file"]
        field_classes = {"visit": VisitChoiceField}
        labels = {
            "test_name": "Test",
            "result_date": "Result date",
            "visit": "From visit",
            "is_abnormal": "Abnormal result",
            "result_text": "Result / values",
            "file": "Report file",
        }
        help_texts = {
            "test_name": "e.g. HbA1c, CBC, Chest X-ray",
            "visit": "Optional: the visit this test was ordered in.",
            "is_abnormal": "Tick if the result needs the doctor's attention.",
        }
        widgets = {
            "result_date": date_input(),
            "result_text": forms.Textarea(attrs={"rows": 4, "placeholder": "e.g. HbA1c 7.8%"}),
            "file": forms.FileInput(),
        }

    def __init__(self, *args, clinic, patient, **kwargs):
        super().__init__(*args, **kwargs)
        visit_field = self.fields["visit"]
        visit_field.queryset = Visit.objects.filter(clinic=clinic, patient=patient).order_by("-visit_date")
        visit_field.empty_label = "Not linked to a visit"

        extensions = allowed_extensions()
        file_field = self.fields["file"]
        file_field.help_text = (
            f"Optional. {', '.join(ext.upper() for ext in extensions)} up to {settings.LAB_UPLOAD_MAX_MB} MB."
        )
        file_field.widget.attrs["accept"] = ",".join(f".{ext}" for ext in extensions)

    def clean_result_date(self):
        result_date = self.cleaned_data.get("result_date")
        if result_date and result_date > timezone.localdate():
            raise forms.ValidationError("The result date can't be in the future.")
        return result_date

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        if isinstance(upload, UploadedFile):
            validate_lab_file(upload)
        return upload
