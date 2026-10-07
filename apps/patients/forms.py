from django import forms
from django.utils import timezone

from apps.core.phone import normalize_mobile, with_ascii_digits

from .models import Patient
from .services import find_possible_duplicates, phone_example

MAX_AGE_YEARS = 120
CSV_MAX_BYTES = 2 * 1024 * 1024  # 2 MB


def years_before(day, years):
    """The same calendar day `years` earlier (29 Feb becomes 28 Feb in non-leap years)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


class PatientForm(forms.ModelForm):
    """Add / edit a patient.

    Pass `clinic` (for phone rules and the duplicate check) and `is_clinician`:
    receptionists' forms leave out blood group, allergies and chronic conditions,
    so their edits can never overwrite (or wipe) clinical information.
    """

    CLINICAL_FIELDS = ("blood_group", "allergies", "chronic_conditions")

    age_years = forms.IntegerField(
        label="Age (if date of birth unknown)",
        min_value=0,
        max_value=MAX_AGE_YEARS,
        required=False,
        help_text="In years. We will save an approximate date of birth.",
        widget=forms.NumberInput(attrs={"inputmode": "numeric"}),
    )
    confirm_not_duplicate = forms.BooleanField(
        label="This is a different person",
        required=False,
        help_text="Tick this only after checking the patients listed above.",
    )

    class Meta:
        model = Patient
        fields = [
            "full_name", "guardian_name", "sex", "date_of_birth",
            "phone", "whatsapp_phone", "reminders_opt_in", "city", "address",
            "blood_group", "allergies", "chronic_conditions",
            "notes",
        ]
        labels = {
            "full_name": "Full name",
            "guardian_name": "Father / husband name",
            "date_of_birth": "Date of birth",
            "phone": "Mobile number",
            "whatsapp_phone": "WhatsApp number",
            "reminders_opt_in": "Agrees to WhatsApp reminders",
            "notes": "Front-desk notes",
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"autocomplete": "off", "autofocus": True}),
            "guardian_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "phone": forms.TextInput(attrs={"type": "tel", "inputmode": "tel", "autocomplete": "off"}),
            "whatsapp_phone": forms.TextInput(attrs={"type": "tel", "inputmode": "tel", "autocomplete": "off"}),
            "address": forms.TextInput(attrs={"autocomplete": "off"}),
            "allergies": forms.Textarea(attrs={"rows": 2}),
            "chronic_conditions": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, clinic, is_clinician, **kwargs):
        super().__init__(*args, **kwargs)
        self.clinic = clinic
        self.is_clinician = is_clinician
        self.possible_duplicates = []
        self._dob_is_estimated = self.instance.dob_is_estimated
        if self.instance.pk is None:
            self.instance.clinic = clinic

        if not is_clinician:
            for name in self.CLINICAL_FIELDS:
                del self.fields[name]

        example = phone_example(clinic.country)
        self.fields["phone"].widget.attrs["placeholder"] = example
        self.fields["phone"].help_text = f"e.g. {example}. Leave empty if the patient has no mobile."
        self.fields["whatsapp_phone"].widget.attrs["placeholder"] = example
        self.fields["sex"].choices = [("", "Choose…")] + list(Patient.Sex.choices)
        if self.instance.dob_is_estimated:
            self.fields["date_of_birth"].help_text = "Approximate (worked out from age)."

    # --- Field checks ---------------------------------------------------------

    def _clean_phone_field(self, name):
        # Urdu / Hindi digits are saved as 0-9 (formatting kept), so search and tel: links work.
        value = with_ascii_digits((self.cleaned_data.get(name) or "").strip())
        if value and not normalize_mobile(value, self.clinic.country):
            raise forms.ValidationError(
                f"This doesn't look like a mobile number. Please write it in full, "
                f"e.g. {phone_example(self.clinic.country)}."
            )
        return value

    def clean_phone(self):
        return self._clean_phone_field("phone")

    def clean_whatsapp_phone(self):
        return self._clean_phone_field("whatsapp_phone")

    def clean_full_name(self):
        return " ".join(self.cleaned_data["full_name"].split())

    def clean_date_of_birth(self):
        dob = self.cleaned_data.get("date_of_birth")
        today = timezone.localdate()
        if dob and dob > today:
            raise forms.ValidationError("Date of birth can't be in the future.")
        if dob and dob < years_before(today, MAX_AGE_YEARS + 1):
            raise forms.ValidationError("Please check the year: this would make the patient over 120.")
        return dob

    def clean(self):
        cleaned = super().clean()
        self._apply_age(cleaned)
        if self.instance.pk is None:
            self._check_duplicates(cleaned)
        return cleaned

    def _apply_age(self, cleaned):
        """Turn 'Age' into an approximate date of birth when no real date was given."""
        age = cleaned.get("age_years")
        dob = cleaned.get("date_of_birth")
        dob_changed = "date_of_birth" in self.changed_data

        if age is None:
            if dob_changed:
                self._dob_is_estimated = False  # a date was typed (or cleared) by hand
            return

        keeping_old_estimate = dob and self.instance.dob_is_estimated and not dob_changed
        if dob and not keeping_old_estimate:
            self.add_error("age_years", "Enter either the date of birth or the age, not both.")
            return
        if "date_of_birth" in self.errors:
            return
        cleaned["date_of_birth"] = years_before(timezone.localdate(), age)
        self._dob_is_estimated = True

    def _check_duplicates(self, cleaned):
        if self.errors:
            return  # fix the basic mistakes first
        number = normalize_mobile(cleaned.get("whatsapp_phone") or cleaned.get("phone"), self.clinic.country)
        self.possible_duplicates = list(
            find_possible_duplicates(
                self.clinic,
                full_name=cleaned.get("full_name"),
                date_of_birth=cleaned.get("date_of_birth"),
                whatsapp_number=number,
            )[:10]
        )
        if self.possible_duplicates and not cleaned.get("confirm_not_duplicate"):
            self.add_error(
                "confirm_not_duplicate",
                "To save, tick this box. If it is the same person, open their record above instead.",
            )

    def save(self, commit=True):
        self.instance.dob_is_estimated = self._dob_is_estimated and self.instance.date_of_birth is not None
        return super().save(commit=commit)


class PatientImportForm(forms.Form):
    # What to do with rows that look like a patient who is already registered.
    # The page only offers this after an upload found some; until then they stop the import.
    SKIP_DUPLICATES = "skip"
    IMPORT_DUPLICATES = "import"

    file = forms.FileField(
        label="CSV file",
        help_text="Saved from Excel or Google Sheets as “CSV UTF-8”. Up to 2 MB (about 5,000 patients).",
        widget=forms.FileInput(attrs={"accept": ".csv,text/csv"}),
    )
    duplicates = forms.ChoiceField(
        label="Patients who look already registered",
        choices=[
            (SKIP_DUPLICATES, "Skip them and import only the new patients"),
            (IMPORT_DUPLICATES, "These are different people: import them too"),
        ],
        required=False,
        widget=forms.RadioSelect,
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Please choose a .csv file (in Excel: File › Save As › CSV UTF-8).")
        if upload.size > CSV_MAX_BYTES:
            raise forms.ValidationError("This file is bigger than 2 MB. Split it into smaller files and import each one.")
        return upload
