"""Re-check every patient's WhatsApp number with the strict mobile rule.

Numbers saved under the old, lenient check (a landline, a digit missing, Urdu or Hindi digits)
could get a wa.me link that reaches nobody, or the wrong person. After this migration such a
patient has no WhatsApp number, so the app shows "call instead". Urdu / Hindi digits in the
typed numbers are also turned into 0-9, so searching for the number finds the patient.
"""

from django.db import migrations

BATCH_SIZE = 500


def recheck_numbers(apps, schema_editor):
    from apps.core.phone import normalize_mobile, with_ascii_digits

    Patient = apps.get_model("patients", "Patient")
    changed = []
    patients = Patient.objects.select_related("clinic").only(
        "pk", "phone", "whatsapp_phone", "whatsapp_number", "clinic__country"
    )
    for patient in patients.iterator(chunk_size=BATCH_SIZE):
        phone = with_ascii_digits(patient.phone)
        whatsapp_phone = with_ascii_digits(patient.whatsapp_phone)
        number = normalize_mobile(whatsapp_phone or phone, patient.clinic.country)
        if (phone, whatsapp_phone, number) != (patient.phone, patient.whatsapp_phone, patient.whatsapp_number):
            patient.phone, patient.whatsapp_phone, patient.whatsapp_number = phone, whatsapp_phone, number
            changed.append(patient)
    Patient.objects.bulk_update(changed, ["phone", "whatsapp_phone", "whatsapp_number"], batch_size=BATCH_SIZE)


class Migration(migrations.Migration):

    dependencies = [
        ("patients", "0002_phone_optional"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(recheck_numbers, migrations.RunPython.noop),
    ]
