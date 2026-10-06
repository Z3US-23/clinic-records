"""Prepare the day's WhatsApp reminders for every active clinic.

    python manage.py generate_reminders
    python manage.py generate_reminders --clinic al-noor-family-clinic   (one clinic, by slug)

The Reminders page and the Today page already do this for their own clinic each
time they open, so this command is a safety net: it makes sure reminders are
ready (and the "To send" count in the menu is right) even on a day nobody opens
those pages first. Running it twice is harmless: nothing is created twice.

Run it once a day, early in the morning, for example:

* Linux server (cron), 6:15 am server time:
      15 6 * * *  cd /srv/clinic-records && .venv/bin/python manage.py generate_reminders
* Render: add a "Cron Job" service using the same repository and environment
  variables, schedule "15 1 * * *" (Render uses UTC, so that is 6:15 am in
  Pakistan, 6:45 am in India) and command "python manage.py generate_reminders".
* Windows (Task Scheduler): Create Basic Task -> Daily -> 6:15 am ->
  Start a program: <project folder>\\.venv\\Scripts\\python.exe
  Arguments: manage.py generate_reminders    Start in: <project folder>

Each clinic uses its own timezone to decide what "today" is.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Clinic
from apps.reminders.services import generate_reminders


class Command(BaseCommand):
    help = "Prepare today's WhatsApp reminders for every active clinic (run once a day)."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", metavar="SLUG", help="Only this clinic (its slug, e.g. al-noor-clinic).")

    def handle(self, *args, **options):
        clinics = Clinic.objects.filter(is_active=True).order_by("name")
        if options["clinic"]:
            clinics = clinics.filter(slug=options["clinic"])
            if not clinics.exists():
                raise CommandError(f"No active clinic with the slug {options['clinic']!r}.")

        failed = []
        total = 0
        for clinic in clinics:
            try:
                created = generate_reminders(clinic)
            except Exception as error:  # one clinic's problem must not stop the others
                failed.append(clinic.name)
                # Only the error type: the details could contain patient names.
                self.stderr.write(self.style.ERROR(f"Clinic {clinic.name}: failed ({error.__class__.__name__})"))
                continue
            total += created
            self.stdout.write(f"Clinic {clinic.name}: {created} new reminder{'' if created == 1 else 's'}")

        self.stdout.write(self.style.SUCCESS(f"Done. {total} new reminder{'' if total == 1 else 's'} in total."))
        if failed:
            raise CommandError(f"Reminders could not be prepared for: {', '.join(failed)}")
