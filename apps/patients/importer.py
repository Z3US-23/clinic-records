"""Bring existing (paper or spreadsheet) patient records in from a CSV file.

All-or-nothing: every row is checked first. If any row has a problem, nothing is
saved and staff get a list of "Row N: problem" to fix in their spreadsheet.

Rows that look like a patient who is already registered (or like an earlier row of the
same file) are listed too, so importing the same file twice never doubles the register.
Staff then choose: skip those rows, or import them anyway. Until they choose, nothing is saved.

Row numbers match the spreadsheet: the header is row 1, the first patient is row 2.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Clinic
from apps.core.phone import normalize_mobile, with_ascii_digits

from .forms import MAX_AGE_YEARS, years_before
from .models import Patient
from .services import DuplicateIndex, phone_example

MAX_ROWS = 5000
BULK_BATCH_SIZE = 500  # patients per INSERT statement when saving

# Column name -> (required?, plain-words description for the help page)
COLUMNS = {
    "full_name": (True, "Patient's full name"),
    "sex": (True, "M, F or O (or Male, Female, Other)"),
    "date_of_birth": (False, "YYYY-MM-DD or DD/MM/YYYY, e.g. 1988-04-15 or 15/04/1988"),
    "age": (False, "Age in years, only if the date of birth is not known"),
    "phone": (False, "Mobile number. Leave empty if the patient has none."),
    "whatsapp_phone": (False, "Only if different from the mobile number"),
    "guardian_name": (False, "Father / husband name"),
    "city": (False, ""),
    "address": (False, ""),
    "allergies": (False, "Drug or food allergies"),
    "chronic_conditions": (False, "e.g. Diabetes type 2, Hypertension"),
    "notes": (False, "Front-desk notes"),
    "mrn": (False, "Your existing file / MR number. Leave empty to get one automatically."),
}
REQUIRED_COLUMNS = [name for name, (required, _) in COLUMNS.items() if required]

SEX_VALUES = {
    "m": Patient.Sex.MALE, "male": Patient.Sex.MALE,
    "f": Patient.Sex.FEMALE, "female": Patient.Sex.FEMALE,
    "o": Patient.Sex.OTHER, "other": Patient.Sex.OTHER,
}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")

# Longest text each column can hold (from the Patient model).
MAX_LENGTHS = {
    name: Patient._meta.get_field(name).max_length
    for name in ("full_name", "guardian_name", "city", "address", "mrn", "phone", "whatsapp_phone")
}

# MR numbers the app hands out itself look like "P-00042" (see Clinic.allocate_mrn).
# Up to 9 digits: a longer made-up number in a file must not push the clinic's counter
# past what the database can store.
AUTO_MRN_RE = re.compile(r"^P-([0-9]{1,9})$")

EXAMPLE_ROWS = {
    "PK": {
        "full_name": "Ayesha Khan", "sex": "F", "date_of_birth": "1988-04-15", "age": "",
        "phone": "0300-1234567", "whatsapp_phone": "", "guardian_name": "Imran Khan", "city": "Lahore",
        "address": "House 12, Street 4, Model Town", "allergies": "Penicillin",
        "chronic_conditions": "Hypertension", "notes": "Prefers Urdu", "mrn": "",
    },
    "IN": {
        "full_name": "Priya Sharma", "sex": "F", "date_of_birth": "15/04/1988", "age": "",
        "phone": "98765 43210", "whatsapp_phone": "", "guardian_name": "Rahul Sharma", "city": "Jaipur",
        "address": "12, MI Road", "allergies": "Penicillin",
        "chronic_conditions": "Hypertension", "notes": "Prefers Hindi", "mrn": "",
    },
}


class ImportFileError(Exception):
    """The file as a whole can't be read (wrong encoding, missing columns, too many rows...)."""


@dataclass
class ImportResult:
    rows: list = field(default_factory=list)  # [(row_number, unsaved Patient)], one per row without errors
    errors: list = field(default_factory=list)  # [(row_number, problem)]: must be fixed in the file
    duplicates: list = field(default_factory=list)  # [(row_number, problem)]: looks already registered

    @property
    def ok(self):
        """The file can be imported as it is: nothing to fix and nobody who looks already registered."""
        return not self.errors and not self.duplicates

    @property
    def patients(self):
        """Every good row's patient, including the ones that look already registered."""
        return [patient for _, patient in self.rows]

    @property
    def new_patients(self):
        """Every good row's patient, minus the ones that look already registered."""
        flagged = {row_number for row_number, _ in self.duplicates}
        return [patient for row_number, patient in self.rows if row_number not in flagged]


def template_csv(country):
    """The downloadable template: header row + one made-up example patient."""
    out = io.StringIO()
    out.write(chr(0xFEFF))  # byte-order mark, so Excel opens the file as UTF-8
    writer = csv.DictWriter(out, fieldnames=list(COLUMNS), lineterminator="\r\n")
    writer.writeheader()
    writer.writerow(EXAMPLE_ROWS.get(country, EXAMPLE_ROWS["PK"]))
    return out.getvalue()


def _decode(raw_bytes):
    try:
        return raw_bytes.decode("utf-8-sig")  # works with and without a BOM
    except UnicodeDecodeError:
        raise ImportFileError(
            "This file is not saved as UTF-8. In Excel choose File › Save As › “CSV UTF-8 (Comma delimited)”, "
            "then upload it again."
        )


def _column_key(header):
    """'Full Name*' / ' full_name ' -> 'full_name'."""
    return re.sub(r"[\s\-]+", "_", (header or "").strip().strip("*").strip().lower())


def _parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_patients_csv(raw_bytes, clinic, created_by=None):
    """Read and check every row. Returns an ImportResult; saves nothing."""
    reader = csv.reader(io.StringIO(_decode(raw_bytes)))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportFileError("The file is empty.")
    except csv.Error:
        raise ImportFileError("This file could not be read as CSV.")

    keys = [_column_key(name) for name in header]
    missing = [name for name in REQUIRED_COLUMNS if name not in keys]
    if missing:
        raise ImportFileError(
            "The first row must contain the column names. Missing: " + ", ".join(missing)
            + ". Download the template to see the exact names."
        )

    result = ImportResult()
    today = timezone.localdate()
    country = clinic.country
    existing_mrns, registered = _registered_patients(clinic)
    file_mrns = {}  # mrn -> row number where first seen
    earlier_rows = DuplicateIndex()  # this file's good rows, labelled with their row numbers
    data_rows = 0

    try:
        for row_number, values in enumerate(reader, start=2):
            if not any(value.strip() for value in values):
                continue  # blank line
            data_rows += 1
            if data_rows > MAX_ROWS:
                raise ImportFileError(
                    f"This file has more than {MAX_ROWS:,} patients. Split it into smaller files and import each one."
                )
            # Columns we don't know are ignored. Missing trailing cells count as empty.
            row = {}
            for key, value in zip(keys, values):
                if key in COLUMNS and key not in row:
                    row[key] = value.strip()
            problems = []
            patient = _build_patient(row, clinic, country, today, problems)
            mrn = row.get("mrn", "")
            if mrn:
                if mrn in existing_mrns:
                    problems.append(f"MR number {mrn} is already used by another patient in this clinic.")
                elif mrn in file_mrns:
                    problems.append(f"MR number {mrn} is also used in row {file_mrns[mrn]}.")
                else:
                    file_mrns[mrn] = row_number
            if problems:
                result.errors.extend((row_number, problem) for problem in problems)
                continue
            patient.created_by = created_by
            result.rows.append((row_number, patient))
            duplicate = _duplicate_problem(patient, registered, earlier_rows)
            if duplicate:
                result.duplicates.append((row_number, duplicate))
            earlier_rows.add(row_number, patient.full_name, patient.date_of_birth, patient.whatsapp_number)
    except csv.Error:
        raise ImportFileError("This file could not be read as CSV. Save it again from Excel as “CSV UTF-8”.")

    if data_rows == 0:
        raise ImportFileError("The file has a header row but no patients under it.")
    return result


def _registered_patients(clinic):
    """One query for the whole file: every MR number in use, and the clinic's active patients.

    Archived patients don't count as "already registered", the same rule as the new-patient form.
    """
    mrns = set()
    registered = DuplicateIndex()
    patients = Patient.objects.filter(clinic=clinic).values_list(
        "mrn", "full_name", "date_of_birth", "whatsapp_number", "is_archived"
    )
    for mrn, full_name, date_of_birth, whatsapp_number, is_archived in patients:
        mrns.add(mrn)
        if not is_archived:
            registered.add(mrn, full_name, date_of_birth, whatsapp_number)
    return mrns, registered


def _duplicate_problem(patient, registered, earlier_rows):
    """Why this row looks like someone already registered (or an earlier row), or None."""
    identity = (patient.full_name, patient.date_of_birth, patient.whatsapp_number)
    match = registered.find(*identity)
    if match:
        mrn, same = match
        return f"{patient.full_name} looks already registered as {mrn} (same name and {same})."
    match = earlier_rows.find(*identity)
    if match:
        row_number, same = match
        return f"Same patient as row {row_number} (same name and {same})."
    return None


def _build_patient(row, clinic, country, today, problems):
    """Check one row and return an unsaved Patient. Problems are appended to `problems`."""
    for name, max_length in MAX_LENGTHS.items():
        if len(row.get(name, "")) > max_length:
            problems.append(f"{name} is too long (max {max_length} characters).")

    full_name = " ".join(row.get("full_name", "").split())
    if not full_name:
        problems.append("full_name is empty.")

    sex = SEX_VALUES.get(row.get("sex", "").lower())
    if sex is None:
        problems.append(f"sex must be M, F or O (found “{row.get('sex', '')}”)." if row.get("sex") else "sex is empty.")

    dob, estimated = None, False
    dob_text, age_text = row.get("date_of_birth", ""), row.get("age", "")
    if dob_text:
        dob = _parse_date(dob_text)
        if dob is None:
            problems.append(f"date_of_birth “{dob_text}” is not a date. Use YYYY-MM-DD or DD/MM/YYYY.")
        elif dob > today:
            problems.append("date_of_birth is in the future.")
        elif dob < years_before(today, MAX_AGE_YEARS + 1):
            problems.append("date_of_birth makes the patient over 120. Please check the year.")
    elif age_text:
        age = _whole_years(age_text)
        if age is not None and age <= MAX_AGE_YEARS:
            dob, estimated = years_before(today, age), True
        else:
            problems.append(f"age must be a whole number from 0 to {MAX_AGE_YEARS} (found “{age_text}”).")

    # The mobile number is optional (some patients have none), but must be a real one if given.
    phone = with_ascii_digits(row.get("phone", ""))
    example = phone_example(country)
    if phone and not normalize_mobile(phone, country):
        problems.append(f"phone “{phone}” is not a mobile number (e.g. {example}).")
    whatsapp_phone = with_ascii_digits(row.get("whatsapp_phone", ""))
    if whatsapp_phone and not normalize_mobile(whatsapp_phone, country):
        problems.append(f"whatsapp_phone “{whatsapp_phone}” is not a mobile number (e.g. {example}).")

    patient = Patient(
        clinic=clinic,
        mrn=row.get("mrn", ""),
        full_name=full_name,
        guardian_name=row.get("guardian_name", ""),
        sex=sex or "",
        date_of_birth=dob,
        dob_is_estimated=estimated,
        phone=phone,
        whatsapp_phone=whatsapp_phone,
        city=row.get("city", ""),
        address=row.get("address", ""),
        allergies=row.get("allergies", ""),
        chronic_conditions=row.get("chronic_conditions", ""),
        notes=row.get("notes", ""),
    )
    patient.set_whatsapp_number()  # needed for the "already registered?" check
    return patient


def _whole_years(text):
    """'42' -> 42. None for anything else, e.g. '4.5', '-3' or '²'.

    isdecimal(), not isdigit(): isdigit() also accepts '²' and '①', which int() can't read.
    (Arabic-Indic and Devanagari digits are decimal, so they still work.) The length check
    keeps int() away from absurdly long numbers.
    """
    if text.isdecimal() and len(text) <= len(str(MAX_AGE_YEARS)):
        return int(text)
    return None


def auto_mrn(number):
    """The automatic MR number for a counter value: 42 -> 'P-00042' (the same as Clinic.allocate_mrn)."""
    return Clinic.format_mrn(number)


def _highest_auto_number(mrns):
    """The biggest counter value among 'P-00042' style MR numbers (0 if there are none)."""
    return max((int(match.group(1)) for match in map(AUTO_MRN_RE.match, mrns) if match), default=0)


def save_imported_patients(clinic, patients):
    """Save the checked patients in one transaction (all or nothing). Returns how many.

    Runs a handful of queries however long the file is: the MR numbers are handed out here
    in one go and the patients are inserted in batches, instead of one save() per patient.
    """
    with transaction.atomic():
        # Lock the clinic row, so nobody else takes an automatic MR number until we commit.
        locked = Clinic.objects.select_for_update().get(pk=clinic.pk)
        in_use = list(Patient.objects.filter(clinic=clinic).values_list("mrn", flat=True))
        in_use += [patient.mrn for patient in patients if patient.mrn]
        # Start after the clinic's counter AND after any "P-00123" number already in use (an
        # earlier import or this file may bring its own), so every number handed out is free
        # and patients added later never get one that is taken.
        counter = max(locked.patient_counter, _highest_auto_number(in_use))
        for patient in patients:
            if not patient.mrn:
                counter += 1
                patient.mrn = auto_mrn(counter)
            patient.set_whatsapp_number()  # bulk_create skips Patient.save(), which normally does this
        Patient.objects.bulk_create(patients, batch_size=BULK_BATCH_SIZE)
        Clinic.objects.filter(pk=clinic.pk).update(patient_counter=counter)
    clinic.patient_counter = counter
    return len(patients)
