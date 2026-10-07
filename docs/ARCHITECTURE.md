# Clinic Records — architecture & conventions

Clinic management web app for independent doctors and small clinics in Pakistan and India:
patient records, medical history, appointments, and WhatsApp follow-up reminders.

**Product decisions (from the founder):**
- First version is demo-ready (fake sample data) but built to pilot-grade standards: real logins,
  clinic isolation, audit log, backups/export.
- Python + Django 5.2 server-rendered pages + a little vanilla JS. Works on phones as an installable web app (PWA).
- WhatsApp reminders are **one-tap**: the app prepares each message; staff tap a button that opens
  WhatsApp (`https://wa.me/<number>?text=...`) with the message ready. No Meta API in v1, but the
  code keeps a channel abstraction so automatic sending can be added later.
- Reminder text in English for now; templates are stored per clinic + language so Urdu can be added.
- Patients confirm or ask to reschedule via a **public tokenized link** in the message (`/c/<token>/`), no login.

## Running

```bash
.venv/Scripts/python manage.py runserver          # Windows venv path
.venv/Scripts/python manage.py test               # all tests
.venv/Scripts/python manage.py seed_demo --reset  # (re)create the demo clinic with fake patients
.venv/Scripts/python manage.py generate_reminders # daily job: prepare WhatsApp reminders
```

**Deploying:** set `SITE_URL` to the public `https://` address (no path) before the first clinic uses reminders.
With `DJANGO_DEBUG` off the app refuses to start without it, because every appointment reminder stores the
patient's confirmation link inside its text. If the domain changes later, keep the old host in
`DJANGO_ALLOWED_HOSTS` until pending reminders have been sent. `manage.py check --deploy` warns (core.W001)
when `SITE_URL`'s host is not in `DJANGO_ALLOWED_HOSTS`.

`seed_demo --reset` recreates **Demo Family Clinic** with three logins: `demo-owner@clinic.test`,
`demo-doctor@clinic.test`, `demo-reception@clinic.test` (password `demo-pass-2026`, or `--password`).
It only ever touches the demo clinic. Demo phone numbers use the unallocated `0390-` prefix so
"Send on WhatsApp" can never reach a real person: **keep it that way**. Its logins and password are public:
**never run it on a database with real patients**.

Public sign-up (`accounts:signup`) is off unless `ALLOW_CLINIC_SIGNUP=True`: turn it on for demo sites only.

## Layout & ownership

```
config/            settings.py, urls.py (root)                     — shared, do not edit casually
apps/core/         dashboard, missed follow-ups, audit log page, export/backup, PWA, seed_demo, shared helpers
   permissions.py  ClinicRequiredMixin, ClinicianRequiredMixin, OwnerRequiredMixin, ClinicScopedMixin,
                   clinic_required decorator, for_clinic(), CLINICAL_ROLES, OWNER_ONLY
   audit.py        log_action(request, Action.X, obj, summary)
   phone.py        normalize_mobile(raw, country): STRICT, for patients' mobile/WhatsApp numbers (PK/IN: exactly
                   10 digits after the code, mobile prefix; foreign numbers only with + or 00);
                   normalize_phone(raw, country): lenient, any phone (clinic landline, tel: links);
                   ascii_digits(text) (digits only, Urdu/Hindi -> 0-9), with_ascii_digits(text) (keeps the formatting),
                   whatsapp_link(number, text), format_phone_display()
   services.py     day_bounds(day), reschedule_requests(clinic, today), missed_follow_ups(clinic, today)
   middleware.py   sets request.clinic / request.membership, activates clinic timezone, no-cache for staff pages
   testing.py      ClinicTestCase + factories (two clinics, owner/doctor/receptionist; fast password hasher)
   templatetags/core_tags.py   |badge_class  |initials  |phone_display  |tel_href:country ("tel:+923001234567")
   tests_smoke.py  every URL name x every role: status codes, roles, clinic isolation, POST-only
apps/accounts/     User (email login), Clinic, Membership(role); login, signup, staff, invitations, clinic settings
   lockout.py      failed sign-in limits (email+IP, email on any IP, IP with any email), shared by the app and admin
   middleware.py   PasswordChangeRequiredMiddleware (see "Accounts" below)
apps/patients/     Patient; list/search, create/edit, profile = medical-history dashboard, CSV import
apps/clinical/     Visit (+vitals, diagnosis, follow_up_date), PrescriptionItem, LabResult; print prescription
apps/appointments/ Appointment; day/week views, booking, status changes; public confirm page
apps/reminders/    Reminder, MessageTemplate; generation service, one-tap send, templates editor
templates/         base.html, base_public.html, includes/*, <app>/*.html, registration/*
static/            css/app.css (design system), js/app.js, icons/
```

Models are defined already — read `apps/<app>/models.py` before writing views.

## Non-negotiable rules (security & privacy)

1. **Clinic isolation.** Every query for clinic data is filtered by `request.clinic`.
   Use `ClinicScopedMixin` (CBVs) or `for_clinic(Model, request)` / `get_object_or_404(Model, pk=pk, clinic=request.clinic)`.
   For models without a `clinic` field (PrescriptionItem) filter through the parent (`visit__clinic=request.clinic`).
   **Form choice fields** (patient, doctor, visit) must have their querysets limited to the current clinic —
   pass `clinic` into the form's `__init__`. A crafted POST with another clinic's id must fail validation.
   Objects from another clinic must return **404** (not 403) so existence isn't leaked.
2. **Roles.** `request.membership.role` is one of `owner`, `doctor`, `receptionist`.

   | Area | owner | doctor | receptionist |
   |---|---|---|---|
   | Patients: list/search/create/edit demographics | ✓ | ✓ | ✓ |
   | Patient profile: contact info, appointments, reminders | ✓ | ✓ | ✓ |
   | Clinical data: visits, vitals, diagnoses, prescriptions, lab results, allergies, chronic conditions | ✓ | ✓ | ✗ (hidden on pages, 403 on URLs) |
   | Appointments: book/edit/status | ✓ | ✓ | ✓ |
   | Reminders: send/skip/edit/custom | ✓ | ✓ | ✓ |
   | Archive patient | ✓ | ✓ | ✗ |
   | Staff, clinic settings, message templates, export, CSV import, audit log | ✓ | ✗ | ✗ |

   Allergies are clinical but safety-critical: receptionists do not see them either (doctors see them in red).
   A membership that is a waiting invitation (`accepted_at` empty) gives no access at all (see "Accounts" below).
   Templates use `is_clinician` / `is_owner` (from the context processor) to hide links/sections;
   views enforce with the mixins (`ClinicianRequiredMixin`, `OwnerRequiredMixin`) or `@clinic_required(roles=...)`.
3. **State changes are POST-only** with CSRF; redirect after POST (PRG). GET must never change data.
   A POST sent twice must not create a record twice: app.js blocks double taps, and the server checks the
   cases that matter (a new-visit form carries a one-time `form_token`; a walk-in for a patient already waiting
   today is not booked again; a CSV import flags patients who are already registered).
4. **Audit.** `log_action()` on: viewing a patient profile or opening its edit form (VIEW), viewing a visit (VIEW), downloading a lab file (VIEW),
   opening the new-visit / edit-visit / edit-lab forms (VIEW, against the Patient, Visit or LabResult, MRN only),
   each page of the visit list (one VIEW row listing the MRNs shown: "Viewed visit list: P-00012, P-00007"),
   create/update/delete of patients, visits, labs, appointments; reminder sent (SEND); export (EXPORT); import (IMPORT);
   login/logout/failed login (a failed sign-in is logged in every clinic the account works at, so its owner sees it);
   switching clinic (LOGOUT in the old clinic, LOGIN in the new one); staff changes. Keep `summary` short and free of
   clinical details (e.g. "Viewed patient P-00012", not the diagnosis).
5. **Uploaded files are private.** No public media URL. Serve via a view that checks clinic + role, uses
   `FileResponse(..., as_attachment=False)` with a safe filename and `X-Content-Type-Options: nosniff`.
   Validate extension (settings.LAB_UPLOAD_EXTENSIONS) and size (settings.LAB_UPLOAD_MAX_MB).
6. **Public confirm page** (`/c/<token>/`) shows only: clinic name/phone, patient first name, appointment date/time,
   doctor name. No MRN, no clinical info. Unknown token → 404. Past/cancelled appointments can't be changed.
7. Never put patient data in URLs' query strings except ids. Never log clinical text to the console.
8. Escape everything (Django autoescape on; no `|safe` on user data). JS builds HTML only with escaping.

## URL contract (names are fixed; other agents link to them)

| name | path | notes |
|---|---|---|
| `core:dashboard` | `/` | "Today" page |
| `core:missed_follow_ups` | `/follow-ups/missed/` | all roles; patients who didn't come back (no clinical text) |
| `core:hide_setup_checklist` | `/setup-checklist/hide/` | owner; POST |
| `core:audit_log` | `/audit-log/` | owner |
| `core:export` | `/export/` | owner; GET page, POST downloads ZIP |
| `core:manifest` | `/manifest.webmanifest` | PWA |
| `core:service_worker` | `/sw.js` | PWA (served from root for scope) |
| `core:offline` | `/offline/` | PWA offline fallback |
| `accounts:login` / `accounts:logout` | `/accounts/login/`, `/accounts/logout/` | logout is POST |
| `accounts:password_change` / `accounts:password_change_done` | `/accounts/password/`… | |
| `accounts:signup` | `/accounts/signup/` | creates clinic + owner; 404 if `settings.ALLOW_CLINIC_SIGNUP` is False |
| `accounts:no_clinic` | `/accounts/no-clinic/` | signed-in user without an active clinic |
| `accounts:switch_clinic` | `/accounts/switch-clinic/<clinic_id>/` | POST |
| `accounts:join <token>` | `/accounts/join/<token>/` | the signed-in invitee only (anyone else: 404); GET shows the invitation, POST `action=accept` with their password, or `action=decline` |
| `accounts:profile` | `/accounts/profile/` | own name, title, qualifications, reg. no. |
| `accounts:staff_list`, `staff_add`, `staff_edit <pk>`, `staff_set_password <pk>` | `/accounts/staff/…` | owner; pk = Membership pk |
| `accounts:staff_invite <pk>` | `/accounts/staff/<pk>/invite/` | owner; POST `action=renew` (new join link) or `action=cancel` |
| `accounts:clinic_settings` | `/accounts/settings/` | owner |
| `patients:list` | `/patients/?q=` | |
| `patients:search_json` | `/patients/search.json?q=` | JSON `{"results":[{"id","name","mrn","phone","age_sex","url"}]}` max 8 |
| `patients:create` | `/patients/new/` | |
| `patients:import`, `patients:import_template` | `/patients/import/…` | owner |
| `patients:detail <pk>` | `/patients/<pk>/` | profile / medical history |
| `patients:update <pk>`, `patients:archive <pk>` | | archive is POST |
| `clinical:visit_list` | `/clinical/visits/` | clinicians |
| `clinical:visit_create <patient_pk>` | `/clinical/patients/<patient_pk>/visits/new/?appointment=<id>` | |
| `clinical:visit_detail <pk>`, `visit_update <pk>` | | |
| `clinical:prescription_print <pk>` | `/clinical/visits/<pk>/prescription/` | printable letterhead |
| `clinical:lab_create <patient_pk>` | | |
| `clinical:lab_update <pk>` | `/clinical/labs/<pk>/edit/` | clinicians; a new file replaces the old one |
| `clinical:lab_file <pk>`, `clinical:lab_delete <pk>` | | delete is POST |
| `appointments:day` | `/appointments/?date=YYYY-MM-DD&doctor=<id>` | |
| `appointments:week` | `/appointments/week/?start=YYYY-MM-DD` | |
| `appointments:create` | `/appointments/new/?patient=<id>&date=YYYY-MM-DD&doctor=<id>` | no `patient` → patient search first |
| `appointments:update <pk>` | | |
| `appointments:set_status <pk>` | POST `status=<value>` | |
| `public:confirm <token>` | `/c/<token>/` | public |
| `reminders:list` | `/reminders/?tab=due|upcoming|sent|skipped` | |
| `reminders:create` | `/reminders/new/?patient=<id>` | custom message |
| `reminders:templates` | `/reminders/templates/` | owner |
| `reminders:send <pk>` | POST → marks sent, 302 to wa.me | |
| `reminders:skip <pk>` | POST | |
| `reminders:update <pk>` | edit message before sending | |

Every name maps to a real view. **Keep every name and path signature**: other apps link to them.
`apps/core/tests_smoke.py` opens every one of them for every role, so a renamed or broken URL fails the tests.

## Cross-app service contract

- `apps.reminders.services.generate_reminders(clinic, today=None) -> int` — idempotent; creates pending reminders
  that are due (appointment reminders N days before, follow-up reminders N days before `Visit.follow_up_date`,
  overdue reminders when a follow-up date passed by `overdue_grace_days(clinic)` = max(1, `clinic.overdue_grace_days`)
  days with no later visit, and no later appointment the patient came to (Waiting / Seen) or is still booked for
  (Booked / Confirmed, today or later); "Wants another time" does not count). Called by the dashboard and the
  reminders page; also by the `generate_reminders` management command (daily cron).
- `apps.reminders.services.came_back_or_still_coming(day_start) -> Q` — that "came back or still coming" rule, shared
  by the reminders, the profile's follow-up status and the Today page. `later_visits(...)` / `booked_appointments(...)`
  are the matching subqueries (`apps.core.services.missed_follow_ups` uses them); `wants_reminders(patient)` is
  "opted in and not archived" (a missing WhatsApp number does not count).
- Skipped reminders have a `Reminder.skip_reason`: `"staff"` (pressed Skip; never comes back) or `"system"` (no longer
  needed; comes back by itself when it is needed again). Blank on older rows counts as staff.
- `apps.reminders.services.refresh_for_appointment(appointment, rescheduled=False)` — appointments app calls this after
  create, edit (pass `rescheduled=True` when date/time or doctor changed) and status changes. Cancelled / no-show /
  seen / waiting / "wants another time", or the patient opted out → a pending appointment reminder is marked
  **Skipped** (never deleted, so the history stays); rescheduled → the reminder is re-prepared with the new time
  (set back to "To send" even if the old one was sent); new → created now if already inside the reminder window;
  booked or confirmed again → a reminder the system skipped comes back (one staff skipped stays skipped).
- `apps.reminders.services.refresh_for_patient(patient)` — patients app calls this after `reminders_opt_in` or
  `is_archived` changes: opted out / archived → pending automatic reminders are skipped by the system at once
  (archived also skips pending custom messages); opted in and active → they come back.
- Sending is refused on the server (not only hidden on the page) for automatic reminders to an opted-out or archived
  patient, and for appointment reminders whose appointment is cancelled, changed or past. Custom messages to an
  opted-out patient are allowed: staff wrote them.
- `apps.reminders.services.refresh_for_visit(visit)` — clinical app calls this after a visit is saved. A new visit also
  makes earlier pending follow-up / missed-follow-up reminders for that patient unnecessary.
- `Appointment.get_confirm_url()` — absolute public link (uses `settings.SITE_URL`).
- `Appointment.arrived_at` — set by `Appointment.save()` when the status becomes Arrived, kept on Seen, cleared otherwise.
  `apps.appointments.scheduling.waiting_room(appointments)` is the one waiting-room order and token rule, used by
  the dashboard and the day view (pass one day's appointments for the whole clinic).
- `Patient.whatsapp_number` — normalized digits, set on save with `apps.core.phone.normalize_mobile` ("" when the
  number is not a valid mobile); `Patient.can_receive_whatsapp`.
- `apps.patients.services.search_patients(queryset, q, country)` — the one patient search (name, MR number, phone
  typed any way: "0300 123", "+92 300…", last digits). The list, top-bar search, booking picker and custom-message
  search all use it.
- `apps.appointments.status` — `STATUS_LABELS` (short staff words: Booked, Confirmed, Wants another time, Waiting,
  Seen, Did not come, Cancelled), `ALLOWED_TRANSITIONS`, `can_change(current, new)`. A visit started from an appointment
  only marks it Seen when `can_change` allows it (a cancelled appointment is never marked Seen).

## Accounts: invitations and owner-set passwords

- **Invitations.** When an owner adds an email that already has an account (someone working at another clinic), the
  membership is created as a waiting invitation: `accepted_at` empty and always `is_active=False`
  (`Membership.save()` enforces it), so it gives no access anywhere. The owner shares the one-time join link
  (valid 7 days; `accounts:staff_invite` makes a new one or cancels it). The person signs in, opens the link and
  types their password again to accept (`accounts:join`). The link is the proof of consent: knowing the password is
  not enough. Until then the inviting clinic only sees the email it typed (`Membership.display_name`), never the
  account's name or sign-in history, and the person is left out of the staff export, the audit-log staff filter and
  the doctors list.
- **Owner-set passwords.** A password a clinic owner chose (a new staff account, or "Set password") sets
  `User.must_change_password`. `PasswordChangeRequiredMiddleware` sends that person to "Change password" before any
  other page; changing it clears the flag.
- **Remaining risk:** an owner can still set the password of someone who works only at their clinic and sign in as
  them. That never reaches another clinic, because joining one needs that clinic's link.

## UI conventions

- Staff pages: `{% extends "base.html" %}`, blocks `title`, `content`, optional `extra_js`, `content_class`
  (`content-narrow` for forms). Public pages: `{% extends "base_public.html" %}`.
- `{% load core_tags %}` for `|badge_class`, `|initials`, `|phone_display`.
- `{% load appointment_tags %}` for `{{ appt.status|status_label }}` (always use it to show an appointment status, never
  `get_status_display`) and `{% appointment_actions appt %}` (the quick status buttons; they post back to the current page).
- Page skeleton:
  ```html
  <div class="page-header">
    <div><div class="breadcrumbs">…</div><h1>Title</h1><p class="subtitle">…</p></div>
    <div class="page-actions"><a class="btn btn-primary" href="…">{% include "includes/icon.html" with name="plus" %}Add</a></div>
  </div>
  <div class="card"><div class="card-header"><h2>…</h2></div><div class="card-body">…</div></div>
  ```
- Components (static/css/app.css): `card card-header card-body card-footer`, `btn btn-primary|secondary|ghost|danger|danger-outline|whatsapp btn-sm btn-lg btn-block`,
  `badge badge-primary|success|warning|danger|info|muted badge-count`, `chip (active)`, `alert alert-info|success|warning|danger`,
  `allergy-banner`, `table-wrap > table.table (table-compact, table-stack for mobile with td[data-label])`, `kv` (dl), `list > list-item`,
  `timeline > timeline-item.is-visit|is-lab|is-appointment > timeline-dot + timeline-date + timeline-card`,
  `stat-grid > stat (stat-warning, stat-danger) > stat-label + stat-value + stat-note`, `tabs > tab.active`, `toolbar`,
  `empty-state`, `avatar (avatar-sm, avatar-lg)`, `patient-banner`, `form`, `form-grid`, `form-grid-3`, `span-2`, `field`,
  `form-actions`, `form-section`, `formset-row`, `grid-2`, `grid-3`, `grid-sidebar`, `stack`, `stack-sm`, `row`, `row-between`,
  `muted`, `subtle`, `text-danger`, `nowrap`, `truncate`, `pre-line`, `inline-form`, `btn-group`, `no-print`, `print-only`, `mt-1..3`, `mb-1..3`,
  (`btn-sm` is 40px tall on phones, a thumb-sized target),
  `visually-hidden` (screen-reader-only text, e.g. the header of an Actions column), `token` (waiting-room number),
  `card-empty` (one quiet line inside a card when there is nothing to list).
- Includes: `includes/icon.html` (`with name="calendar" size="sm|lg"`; names listed in the file),
  `includes/form_fields.html` (`with form=form grid=True`), `includes/field.html` (`with field=form.x`),
  `includes/pagination.html` (needs `page_obj`), `includes/empty_state.html`, `includes/patient_banner.html` (`with patient=p`).
- Destructive buttons: `<form method="post" data-confirm="Cancel this appointment?">` — app.js asks first.
- Double taps: app.js ignores a second submit of a POST form while the first is on its way (the pressed button
  gets `.is-busy`; the lock lifts after 10 s or on Back). A POST form that does not leave the page (a file
  download) opts out with `data-allow-resubmit`.
- Dates shown like `6 Oct 2026` (`|date:"j M Y"`), times with `|time:"g:i a"` (Django prints this as `3:30 p.m.`).
  WhatsApp message text is plain text written by `apps.reminders.services.format_time` and reads `3:30 pm`.
  HTML5 `type="date"`/`type="time"` inputs in forms.
- Use Django messages for feedback after actions (`messages.success(request, "Appointment booked.")`).
- Write for clinic staff: plain words ("Seen", "Did not come", "To send"), no jargon.

## Tests

`apps/<app>/tests.py` (or a `tests/` package). Subclass `apps.core.testing.ClinicTestCase` (it switches to a fast
password hasher for tests only; production hashing is unchanged). `force_login` writes a "Signed in" audit entry, so
tests that count audit rows filter by action.
Every feature must have tests for: happy path, clinic isolation (other clinic → 404), role restrictions, and POST-only actions.
