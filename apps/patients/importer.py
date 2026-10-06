"""Bring existing (paper or spreadsheet) patient records in from a CSV file.

All-or-nothing: every row is checked first. If any row has a problem, nothing is
saved and staff get a list of "Row N: problem" to fix in their spreadsheet.

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
from apps.core.phone import normalize_phone

from .forms import MAX_AGE_YEARS, years_before
from .models import Patient
from .services import phone_example

MAX_ROWS = 5000

# Column name -> (required?, plain-words description for the help page)
COLUMNS = {
    "full_name": (True, "Patient's full name"),
    "sex": (True, "M, F or O (or Male, Female, Other)"),
    "date_of_birth": (False, "YYYY-MM-DD or DD/MM/YYYY, e.g. 1988-04-15 or 15/04/1988"),
    "age": (False, "Age in years, only if the date of birth is not known"),
    "phone": (True, "Mobile number"),
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
AUTO_MRN_RE = re.compile(r"^P-(\d+)$")

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
    patients: list = field(default_factory=list)  # unsaved Patient objects, one per good row
    errors: list = field(default_factory=list)  # [(row_number, problem), ...]

    @property
    def ok(self):
        return not self.errors


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
    existing_mrns = set(Patient.objects.filter(clinic=clinic).values_list("mrn", flat=True))
    file_mrns = {}  # mrn -> row number where first seen
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
            for problem in problems:
                result.errors.append((row_number, problem))
            if not problems:
                patient.created_by = created_by
                result.patients.append(patient)
    except csv.Error:
        raise ImportFileError("This file could not be read as CSV. Save it again from Excel as “CSV UTF-8”.")

    if data_rows == 0:
        raise ImportFileError("The file has a header row but no patients under it.")
    return result


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
        if age_text.isdigit() and int(age_text) <= MAX_AGE_YEARS:
            dob, estimated = years_before(today, int(age_text)), True
        else:
            problems.append(f"age must be a whole number from 0 to {MAX_AGE_YEARS} (found “{age_text}”).")

    phone = row.get("phone", "")
    example = phone_example(country)
    if not phone:
        problems.append("phone is empty.")
    elif not normalize_phone(phone, country):
        problems.append(f"phone “{phone}” is not a mobile number (e.g. {example}).")
    whatsapp_phone = row.get("whatsapp_phone", "")
    if whatsapp_phone and not normalize_phone(whatsapp_phone, country):
        problems.append(f"whatsapp_phone “{whatsapp_phone}” is not a mobile number (e.g. {example}).")

    return Patient(
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


def save_imported_patients(clinic, patients):
    """Save every checked patient in one transaction (all or nothing). Returns how many."""
    with transaction.atomic():
        taken = set(Patient.objects.filter(clinic=clinic).values_list("mrn", flat=True))
        taken.update(p.mrn for p in patients if p.mrn)
        _keep_auto_mrns_ahead_of(clinic, taken)
        for patient in patients:
            if not patient.mrn:
                patient.mrn = _next_free_mrn(clinic, taken)
                taken.add(patient.mrn)
            patient.save()  # also works out the WhatsApp number
    return len(patients)


def _next_free_mrn(clinic, taken):
    mrn = clinic.allocate_mrn()
    while mrn in taken:
        mrn = clinic.allocate_mrn()
    return mrn


def _keep_auto_mrns_ahead_of(clinic, taken):
    """If the file brings its own 'P-00123' style numbers, move the clinic's counter past them,
    so patients added later never get a number that is already in use."""
    highest = max((int(m.group(1)) for m in map(AUTO_MRN_RE.match, taken) if m), default=0)
    Clinic.objects.filter(pk=clinic.pk, patient_counter__lt=highest).update(patient_counter=highest)
    clinic.refresh_from_db(fields=["patient_counter"])
