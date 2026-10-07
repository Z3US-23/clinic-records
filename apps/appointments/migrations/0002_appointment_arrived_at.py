from django.db import migrations, models
from django.db.models import F


def fill_arrived_at(apps, schema_editor):
    """Patients already waiting: the last change (the "Arrived" tap) is the best arrival time we have."""
    Appointment = apps.get_model("appointments", "Appointment")
    Appointment.objects.filter(status="arrived", arrived_at__isnull=True).update(arrived_at=F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="arrived_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(fill_arrived_at, migrations.RunPython.noop),
    ]
