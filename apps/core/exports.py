"""Full clinic export ("backup"): one ZIP file of CSV spreadsheets. Used by the core:export page.

What goes in the ZIP:
    patients.csv, visits.csv, prescriptions.csv, lab_results.csv,
    appointments.csv, reminders.csv, staff.csv, README.txt
    labs/<id>-<original file name>   (only when lab report files are requested)

Rules:
  * Only ONE clinic's data, always. Every query below is filtered by `clinic`.
  * No secrets: no passwords, no appointment confirmation tokens. Reminder messages contain the
    patient's confirmation link (/c/<token>/), so the token is cut out of every message.
  * Spreadsheet safety: a cell that starts with = + - @ (or a tab / carriage return) gets a leading
    apostrophe, so Excel shows it as text instead of running it as a formula ("CSV injection").
  * CSVs are UTF-8 with a BOM so Excel shows Urdu and Hindi names correctly.
  * The ZIP is built in a SpooledTemporaryFile: small exports stay in memory, big ones spill to a
    temporary file on disk instead of using up the server's memory.
"""

import csv
import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from tempfile import SpooledTemporaryFile
from zoneinfo import ZoneInfo

from django.core.exceptions import SuspiciousFileOperation
from django.db.models import F
from django.utils import timezone
from django.utils.text import get_valid_filename

from apps.accounts.models import Membership
from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem, Visit
from apps.patients.models import Patient
from apps.reminders.models import Reminder

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
SPOOL_MAX_BYTES = 20 * 1024 * 1024  # keep up to 20 MB in memory, then use a temporary file
CHUNK_SIZE = 1000  # rows fetched from the database at a time
COPY_BUFFER = 1024 * 1024

# A patient's confirmation link, /c/<token>/, wherever it appears in a message (whatever the site
# address was when the message was written, and even if staff pasted it into a custom message).
# Real tokens are 32 characters from secrets.token_urlsafe(24).
CONFIRM_LINK_RE = re.compile(r"/c/[A-Za-z0-9_-]{16,}/?")
CONFIRM_LINK_REMOVED = "/c/(link removed)/"


@dataclass
class ExportResult:
    file: SpooledTemporaryFile  # positioned at the start, ready to send
    filename: str
    row_counts: dict = field(default_factory=dict)  # "patients.csv" -> number of rows
    lab_files_added: int = 0
    lab_files_missing: int = 0


def safe_cell(value, tz=None):
    """Turn one value into CSV text, protected against spreadsheet formula injection."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return timezone.localtime(value, tz).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def export_counts(clinic):
    """How many records each part of the export will hold (shown on the export page)."""
    return {
        "patients": Patient.objects.filter(clinic=clinic).count(),
        "visits": Visit.objects.filter(clinic=clinic).count(),
        "prescriptions": PrescriptionItem.objects.filter(visit__clinic=clinic).count(),
        "lab_results": LabResult.objects.filter(clinic=clinic).count(),
        "lab_files": LabResult.objects.filter(clinic=clinic).exclude(file="").count(),
        "appointments": Appointment.objects.filter(clinic=clinic).count(),
        "reminders": Reminder.objects.filter(clinic=clinic).count(),
        "staff": _staff(clinic).count(),
    }


def build_clinic_export(clinic, *, include_lab_files=False):
    """Build the ZIP for one clinic and return an ExportResult (the caller sends `result.file`)."""
    tz = ZoneInfo(clinic.timezone)
    exported_at = timezone.localtime(timezone.now(), tz)
    staff_names = {m.user_id: m.display_name for m in Membership.objects.filter(clinic=clinic).select_related("user")}

    def person(user):
        """'Dr. Sara Ahmed' for clinic staff; plain name for anyone who has since left."""
        if user is None:
            return ""
        return staff_names.get(user.pk) or user.full_name

    spool = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    result = ExportResult(file=spool, filename=f"{clinic.slug}-export-{exported_at:%Y%m%d}.zip")

    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Lab files first, so lab_results.csv can say which file in the ZIP belongs to which result.
        files_in_zip = {}
        if include_lab_files:
            files_in_zip, missing = _write_lab_files(zf, clinic, exported_at)
            result.lab_files_added, result.lab_files_missing = len(files_in_zip), missing

        tables = [
            ("patients.csv", PATIENT_COLUMNS, _patient_rows(clinic)),
            ("visits.csv", VISIT_COLUMNS, _visit_rows(clinic, person)),
            ("prescriptions.csv", PRESCRIPTION_COLUMNS, _prescription_rows(clinic)),
            ("lab_results.csv", LAB_COLUMNS, _lab_rows(clinic, files_in_zip)),
            ("appointments.csv", APPOINTMENT_COLUMNS, _appointment_rows(clinic, person)),
            ("reminders.csv", REMINDER_COLUMNS, _reminder_rows(clinic, person)),
            ("staff.csv", STAFF_COLUMNS, _staff_rows(clinic)),
        ]
        for filename, columns, rows in tables:
            result.row_counts[filename] = _write_csv(zf, filename, columns, rows, tz)

        zf.writestr("README.txt", _readme(clinic, exported_at, include_lab_files, result).encode("utf-8"))

    spool.seek(0)
    return result


# --- CSV writing ----------------------------------------------------------------------------

def _write_csv(zf, filename, columns, rows, tz):
    """Write one CSV file into the ZIP and return how many data rows it has."""
    count = 0
    # utf-8-sig adds the byte-order mark Excel needs to detect UTF-8.
    with io.TextIOWrapper(zf.open(filename, "w"), encoding="utf-8-sig", newline="") as text:
        writer = csv.writer(text)
        writer.writerow([name for name, _ in columns])
        for row in rows:
            writer.writerow([safe_cell(value, tz) for value in row])
            count += 1
    return count


# Each table: list of (column name, note for README). Row generators below yield values in this order.

PATIENT_COLUMNS = [
    ("patient_id", "Internal number; use it (or mr_number) to match rows in the other files"),
    ("mr_number", "Medical record number shown in the app, e.g. P-00012"),
    ("full_name", ""),
    ("father_or_husband_name", ""),
    ("sex", ""),
    ("date_of_birth", "YYYY-MM-DD; empty if unknown"),
    ("dob_is_estimated", "yes = worked out from an age the patient gave"),
    ("phone", "As typed by staff"),
    ("whatsapp_phone", "Only filled if different from phone"),
    ("whatsapp_number", "International digits used for WhatsApp, e.g. 923001234567"),
    ("agrees_to_reminders", "yes / no"),
    ("address", ""),
    ("city", ""),
    ("blood_group", ""),
    ("allergies", ""),
    ("chronic_conditions", ""),
    ("front_desk_notes", "Non-clinical notes"),
    ("archived", "yes = patient was archived (hidden from lists) but the record is kept"),
    ("added_on", "Date and time the record was created"),
]


def _patient_rows(clinic):
    patients = Patient.objects.filter(clinic=clinic).order_by("mrn", "pk")
    for p in patients.iterator(chunk_size=CHUNK_SIZE):
        yield [
            p.pk, p.mrn, p.full_name, p.guardian_name, p.get_sex_display(), p.date_of_birth, p.dob_is_estimated,
            p.phone, p.whatsapp_phone, p.whatsapp_number, p.reminders_opt_in, p.address, p.city,
            p.blood_group, p.allergies, p.chronic_conditions, p.notes, p.is_archived, p.created_at,
        ]


VISIT_COLUMNS = [
    ("visit_id", "Links to prescriptions.csv and lab_results.csv"),
    ("visit_date", "Date and time of the consultation"),
    ("patient_id", ""),
    ("mr_number", ""),
    ("patient_name", ""),
    ("doctor", ""),
    ("appointment_id", "Links to appointments.csv when the visit came from a booking"),
    ("presenting_complaint", ""),
    ("history", ""),
    ("examination", ""),
    ("bp_systolic", "mmHg"),
    ("bp_diastolic", "mmHg"),
    ("pulse_bpm", "beats per minute"),
    ("temperature_c", "degrees Celsius"),
    ("weight_kg", ""),
    ("height_cm", ""),
    ("spo2_percent", "oxygen saturation"),
    ("blood_sugar_mg_dl", "mg/dL"),
    ("diagnosis", ""),
    ("advice_plan", ""),
    ("follow_up_date", "When the doctor asked the patient to come back"),
    ("last_changed", ""),
]


def _visit_rows(clinic, person):
    visits = Visit.objects.filter(clinic=clinic).select_related("patient", "doctor").order_by("visit_date", "pk")
    for v in visits.iterator(chunk_size=CHUNK_SIZE):
        yield [
            v.pk, v.visit_date, v.patient_id, v.patient.mrn, v.patient.full_name, person(v.doctor), v.appointment_id,
            v.chief_complaint, v.history, v.examination, v.bp_systolic, v.bp_diastolic, v.pulse, v.temperature_c,
            v.weight_kg, v.height_cm, v.spo2, v.blood_sugar, v.diagnosis, v.plan, v.follow_up_date, v.updated_at,
        ]


PRESCRIPTION_COLUMNS = [
    ("visit_id", "The visit this medicine was prescribed in"),
    ("visit_date", ""),
    ("patient_id", ""),
    ("mr_number", ""),
    ("patient_name", ""),
    ("medicine", ""),
    ("dose", ""),
    ("frequency", "e.g. 1+0+1 = morning and night"),
    ("duration", ""),
    ("instructions", ""),
]


def _prescription_rows(clinic):
    # PrescriptionItem has no clinic field: filter through its visit.
    items = (
        PrescriptionItem.objects.filter(visit__clinic=clinic)
        .select_related("visit__patient")
        .order_by("visit__visit_date", "visit_id", "order", "pk")
    )
    for item in items.iterator(chunk_size=CHUNK_SIZE):
        visit = item.visit
        yield [
            visit.pk, visit.visit_date, visit.patient_id, visit.patient.mrn, visit.patient.full_name,
            item.medicine, item.dose, item.frequency, item.duration, item.instructions,
        ]


LAB_COLUMNS = [
    ("lab_result_id", ""),
    ("result_date", ""),
    ("patient_id", ""),
    ("mr_number", ""),
    ("patient_name", ""),
    ("visit_id", "Empty if the result was not added during a visit"),
    ("test_name", ""),
    ("result", ""),
    ("abnormal", "yes = marked abnormal by the doctor"),
    ("report_file", "Name of the uploaded PDF/photo, if any"),
    ("file_in_this_zip", "Path of the report inside this ZIP (only when lab files were included)"),
]


def _lab_rows(clinic, files_in_zip):
    labs = LabResult.objects.filter(clinic=clinic).select_related("patient").order_by("result_date", "pk")
    for lab in labs.iterator(chunk_size=CHUNK_SIZE):
        report_name = lab.original_filename or (os.path.basename(lab.file.name) if lab.file else "")
        yield [
            lab.pk, lab.result_date, lab.patient_id, lab.patient.mrn, lab.patient.full_name, lab.visit_id,
            lab.test_name, lab.result_text, lab.is_abnormal, report_name, files_in_zip.get(lab.pk, ""),
        ]


APPOINTMENT_COLUMNS = [
    ("appointment_id", ""),
    ("date_time", ""),
    ("duration_minutes", ""),
    ("patient_id", ""),
    ("mr_number", ""),
    ("patient_name", ""),
    ("doctor", "Empty = any available doctor"),
    ("reason", ""),
    ("status", "Scheduled, Confirmed by patient, Wants another time, Arrived, Seen, Did not come, Cancelled"),
    ("patient_message", "Message the patient left on the confirmation page"),
    ("patient_replied_at", ""),
    ("booked_at", ""),
]


def _appointment_rows(clinic, person):
    appointments = (
        Appointment.objects.filter(clinic=clinic).select_related("patient", "doctor").order_by("scheduled_at", "pk")
    )
    for a in appointments.iterator(chunk_size=CHUNK_SIZE):
        # The confirmation token is a secret link: it is deliberately not exported.
        yield [
            a.pk, a.scheduled_at, a.duration_minutes, a.patient_id, a.patient.mrn, a.patient.full_name,
            person(a.doctor), a.reason, a.get_status_display(), a.patient_note, a.patient_responded_at, a.created_at,
        ]


REMINDER_COLUMNS = [
    ("reminder_id", ""),
    ("kind", "Appointment reminder, Follow-up due, Missed follow-up or Custom message"),
    ("status", "To send, Sent or Skipped"),
    ("due_date", "Day the message should go out"),
    ("patient_id", ""),
    ("mr_number", ""),
    ("patient_name", ""),
    ("appointment_id", ""),
    ("visit_id", ""),
    ("message", "The WhatsApp text (the patient's confirmation link is removed)"),
    ("channel", ""),
    ("sent_at", ""),
    ("sent_by", ""),
    ("prepared_at", ""),
]


def without_confirm_links(message, token=""):
    """The message with every confirmation link (and the appointment's own token) taken out."""
    text = CONFIRM_LINK_RE.sub(CONFIRM_LINK_REMOVED, message or "")
    if token:  # belt and braces: the token on its own, e.g. if the link was mangled when edited
        text = text.replace(token, "(removed)")
    return text


def _reminder_rows(clinic, person):
    reminders = (
        Reminder.objects.filter(clinic=clinic)
        .select_related("patient", "sent_by")
        .annotate(confirm_token=F("appointment__confirm_token"))
        .order_by("due_date", "pk")
    )
    for r in reminders.iterator(chunk_size=CHUNK_SIZE):
        # The confirmation link inside the message is a secret: it is deliberately not exported.
        yield [
            r.pk, r.get_kind_display(), r.get_status_display(), r.due_date, r.patient_id, r.patient.mrn,
            r.patient.full_name, r.appointment_id, r.visit_id, without_confirm_links(r.message, r.confirm_token),
            r.get_channel_display(), r.sent_at, person(r.sent_by), r.created_at,
        ]


STAFF_COLUMNS = [
    ("name", ""),
    ("title", "e.g. Dr."),
    ("email", "Used to sign in. Passwords are never exported."),
    ("role", "Clinic owner, Doctor or Receptionist"),
    ("qualifications", ""),
    ("registration_number", "PMDC / NMC number"),
    ("active", "no = can no longer sign in to this clinic"),
    ("added_on", ""),
]


def _staff(clinic):
    """The clinic's staff. Waiting invitations are left out: until the person accepts, their name
    and account belong to them, not to the clinic that invited them."""
    return Membership.objects.filter(clinic=clinic, accepted_at__isnull=False)


def _staff_rows(clinic):
    memberships = _staff(clinic).select_related("user").order_by("user__full_name")
    for m in memberships:
        yield [
            m.user.full_name, m.title, m.user.email, m.get_role_display(), m.qualifications, m.registration_number,
            m.is_active and m.user.is_active, m.created_at,
        ]


# --- Lab report files -----------------------------------------------------------------------

def lab_file_arcname(lab):
    """Path inside the ZIP: labs/<id>-<original file name>, made safe (no folders, odd characters)."""
    original = lab.original_filename or os.path.basename(lab.file.name)
    original = os.path.basename(original.replace("\\", "/"))
    try:
        safe = get_valid_filename(original)
    except SuspiciousFileOperation:
        safe = "report"
    stem, ext = os.path.splitext(safe)
    return f"labs/{lab.pk}-{stem[:80]}{ext[:10]}"


def _write_lab_files(zf, clinic, exported_at):
    """Copy this clinic's uploaded lab reports into the ZIP. Returns ({lab_id: path}, number missing)."""
    written, missing = {}, 0
    labs = LabResult.objects.filter(clinic=clinic).exclude(file="").order_by("pk")
    for lab in labs.iterator(chunk_size=CHUNK_SIZE):
        arcname = lab_file_arcname(lab)
        info = zipfile.ZipInfo(arcname, date_time=exported_at.timetuple()[:6])
        info.compress_type = zipfile.ZIP_STORED  # PDFs and photos are already compressed
        try:
            source = lab.file.open("rb")
        except OSError:  # file missing on disk
            missing += 1
            continue
        with source, zf.open(info, "w") as target:
            shutil.copyfileobj(source, target, COPY_BUFFER)
        written[lab.pk] = arcname
    return written, missing


# --- README ---------------------------------------------------------------------------------

def _readme(clinic, exported_at, include_lab_files, result):
    lines = [
        "Clinic Records - data export",
        "============================",
        "",
        f"Clinic:    {clinic.name}",
        f"Exported:  {exported_at:%Y-%m-%d %H:%M} ({clinic.timezone})",
    ]
    if include_lab_files:
        lines.append(f"Lab report files included: yes ({result.lab_files_added} files, in the labs folder)")
        if result.lab_files_missing:
            lines.append(f"  Note: {result.lab_files_missing} report file(s) could not be found on the server.")
    else:
        lines.append("Lab report files included: no (tick the box on the export page to include them)")

    lines += ["", "Files and row counts", "--------------------"]
    for filename, count in result.row_counts.items():
        lines.append(f"{filename:<20} {count:>7} rows")

    lines += [
        "",
        "How to open these files",
        "-----------------------",
        "- Double-click a .csv file to open it in Excel, or import it into Google Sheets.",
        "  The files are UTF-8 (with BOM), so Urdu and Hindi names show correctly.",
        f"- Dates are written YYYY-MM-DD; times YYYY-MM-DD HH:MM in the clinic's time zone ({clinic.timezone}).",
        "- patient_id (or mr_number) links rows between files. visit_id links prescriptions and",
        "  lab results to the visit they belong to.",
        "- Any cell that starts with = + - or @ has an apostrophe (') added in front, so spreadsheet",
        "  programs show it as text and never run it as a formula. For example a phone number",
        "  typed as +92 300 1234567 appears as '+92 300 1234567.",
        "- Passwords and the secret appointment confirmation links are never exported.",
        "",
        "Column notes",
        "------------",
    ]
    for filename, columns in [
        ("patients.csv", PATIENT_COLUMNS),
        ("visits.csv", VISIT_COLUMNS),
        ("prescriptions.csv", PRESCRIPTION_COLUMNS),
        ("lab_results.csv", LAB_COLUMNS),
        ("appointments.csv", APPOINTMENT_COLUMNS),
        ("reminders.csv", REMINDER_COLUMNS),
        ("staff.csv", STAFF_COLUMNS),
    ]:
        lines.append(f"{filename}:")
        for name, note in columns:
            if note:
                lines.append(f"  {name}: {note}")
        lines.append("")

    lines += [
        "Keep this file safe",
        "-------------------",
        "It contains private medical records. Store it on a password-protected or encrypted drive,",
        "do not send it over WhatsApp or email, and delete old copies you no longer need.",
        "",
    ]
    return "\r\n".join(lines)  # Windows line endings so Notepad shows it properly
