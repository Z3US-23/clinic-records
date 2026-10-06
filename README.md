# Clinic Records

Patient records, medical history, appointments and WhatsApp reminders for independent
doctors and small clinics in Pakistan and India.

Doctors in small clinics often can't see a patient's history during a consultation: records
sit in paper files, nobody tracks follow-ups, and patients miss check-ups. Clinic Records keeps
every visit, prescription and lab result in one place, and each morning it prepares the
WhatsApp reminders the receptionist needs to send.

## What it does

| Area | Features |
|---|---|
| **Patients** | Search by name, phone or MR number from any page · profile with full medical history timeline · allergies shown in red · duplicate-patient warning · CSV import to move paper records in |
| **Visits** | Presenting complaint, examination, vitals (BP, pulse, temp, SpO₂, weight/BMI, sugar) · diagnosis & plan · prescription with "repeat last prescription" · **printable prescription on clinic letterhead** · follow-up date |
| **Lab results** | Results with values, abnormal flag, PDF/photo of the report (stored privately, only viewable by doctors) |
| **Appointments** | Day and week schedule · walk-ins · waiting room with token numbers · statuses (Arrived, Seen, Did not come…) · double-booking check |
| **Reminders** | Appointment reminders, follow-up reminders and missed-follow-up alerts prepared automatically · **one-tap WhatsApp send** · patient can confirm or ask for another time from a link · editable message templates |
| **Clinic** | Roles (owner, doctor, receptionist; receptionists can't see clinical notes) · several clinics per doctor · audit log of who viewed what · full data export (ZIP of CSVs) · installable on phones (PWA) |

### How one-tap WhatsApp reminders work (v1)

1. The app works out who needs a reminder today and writes the message for them.
2. The receptionist opens **Reminders** and taps **Send on WhatsApp**.
3. WhatsApp opens with the patient's chat and the message already typed; they press send.
4. The message includes a private link. The patient taps it and chooses **Yes, I will come**
   or **I need another time**, and the schedule updates.

There is no Meta approval, no per-message fee and no setup. The code keeps sending behind a small
"channel" layer (`apps/reminders/channels.py`), so fully automatic sending through the WhatsApp
Business Cloud API can be added later without rewriting the reminder logic.

## Run it on your computer (Windows)

You need Python 3.12. In a terminal opened in this folder:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

```bash
copy .env.example .env
```

```bash
.venv\Scripts\python manage.py migrate
```

```bash
.venv\Scripts\python manage.py seed_demo
```

```bash
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000. `seed_demo` creates **Demo Family Clinic** with about 60 fake patients
and prints three demo logins (owner, doctor, receptionist). Demo phone numbers use the unallocated
`0390` prefix, so tapping "Send on WhatsApp" in the demo can never message a real person.

To start the demo again from scratch (for example the morning of a demo, so "today" has appointments):

```bash
.venv\Scripts\python manage.py seed_demo --reset
```

Run the tests:

```bash
.venv\Scripts\python manage.py test
```

## Daily reminder job

Reminders are prepared automatically whenever someone opens the dashboard or the Reminders page.
To also prepare them early each morning (recommended once live), schedule this command to run
daily at about 7 am:

```bash
python manage.py generate_reminders
```

On Render use a Cron Job; on a Linux server use `cron`; on Windows use Task Scheduler.

## Putting it online (pilot)

The app is a standard Django project. Example on [Render](https://render.com):

1. Push this folder to a GitHub repository.
2. Create a **PostgreSQL** database on Render.
3. Create a **Web Service** from the repo:
   - Build command: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate && python manage.py createcachetable`
   - Start command: `gunicorn config.wsgi`
4. Set environment variables (see `.env.example`): `DJANGO_SECRET_KEY` (long random string),
   `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `SITE_URL`,
   and `ALLOW_CLINIC_SIGNUP=False` if you onboard clinics yourself. Also:
   - `DJANGO_USE_X_FORWARDED_FOR=True`: the app runs behind Render's proxy, so this makes the audit log
     and the failed-sign-in lockout see each person's real IP address.
   - `DJANGO_CACHE_URL=db`: the lockout counts failed sign-ins in a cache shared by all server processes
     (the `createcachetable` step above creates its table). A `redis://` URL also works.
5. Lab report files are saved to `MEDIA_ROOT`. Most hosts wipe the disk on every deploy, so attach
   a persistent disk and point `MEDIA_ROOT` at it (or add S3-compatible storage) **before** real
   reports are uploaded.
6. Turn on the database's automatic daily backups.

`DJANGO_DEBUG` is off by default, so production gets HTTPS redirects, secure cookies and HSTS.

## Privacy & security

Patient records are sensitive health data (India's DPDP Act 2023; Pakistan's privacy rules are
tightening). Built in from day one:

- Every clinic's data is isolated; requests for another clinic's records return "not found".
- Receptionists manage contacts, appointments and reminders but never see clinical notes,
  prescriptions, lab results or allergies.
- Audit log of sign-ins, record views, changes, exports and messages sent.
- Lab reports are never publicly reachable; they are served only to signed-in doctors of that clinic.
- Failed-login lockout, strong password rules, staff pages never cached by the browser.
- Patient-facing confirmation pages show only first name, time and clinic, nothing medical.
- Patients can opt out of reminders, and that choice is recorded.

Before a real pilot: sign a simple data-processing agreement with the clinic, host in a region
you're comfortable with, and keep backups encrypted.

## Project structure

```
config/              settings and root URLs
apps/core/           dashboard, audit log, export, PWA, demo data, shared helpers
apps/accounts/       users, clinics, roles, sign-in, staff, clinic settings
apps/patients/       patient records, search, profile/history, CSV import
apps/clinical/       visits, vitals, prescriptions, lab results, printing
apps/appointments/   schedule, booking, waiting room, patient confirmation link
apps/reminders/      reminder rules, WhatsApp one-tap sending, message templates
templates/ static/   pages, design system (static/css/app.css), small JS
docs/ARCHITECTURE.md rules every part of the code follows
```

## Roadmap ideas

- Urdu / Roman Urdu / Hindi reminder templates (templates are already stored per language)
- Automatic WhatsApp sending through the Business Cloud API, and SMS fallback
- Medicine list with common brands and dosages, and ICD-10 diagnosis codes
- Patient-facing history PDF, vaccination schedules for children
- Simple billing and receipts
