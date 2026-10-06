"""Message wording: the safe placeholder formatter, default templates, dates/times and channels."""

from datetime import date, time
from urllib.parse import quote, unquote

from django.test import SimpleTestCase

from apps.core.phone import whatsapp_link
from apps.reminders import services
from apps.reminders.channels import WhatsAppCloudAPIChannel, WhatsAppLinkChannel, get_channel
from apps.reminders.models import MessageTemplate, ReminderKind
from apps.reminders.services import (
    DEFAULT_TEMPLATES,
    PLACEHOLDERS,
    format_date,
    format_time,
    get_template_body,
    render_message,
    unknown_placeholders,
)

from .base import ReminderTestCase

CONTEXT = {
    "patient_name": "Ali Raza",
    "first_name": "Ali",
    "clinic_name": "Al-Noor",
    "clinic_phone": "042-111",
    "doctor_name": "Dr. Bilal",
    "date": "Wed 7 Oct",
    "time": "3:30 pm",
    "confirm_link": "https://example.test/c/abc/",
}


class SafeFormatterTests(SimpleTestCase):
    def test_known_placeholders_are_filled(self):
        body = "Hi {first_name} ({patient_name}), {clinic_name} {clinic_phone} {doctor_name} {date} {time} {confirm_link}"
        self.assertEqual(
            render_message(body, CONTEXT),
            "Hi Ali (Ali Raza), Al-Noor 042-111 Dr. Bilal Wed 7 Oct 3:30 pm https://example.test/c/abc/",
        )

    def test_malicious_or_malformed_placeholders_are_left_exactly_as_typed(self):
        nasty = [
            "{first_name.__class__}",
            "{first_name.__class__.__mro__[1].__subclasses__()}",
            "{first_name[0]}",
            "{patient_name.__init__.__globals__}",
            "{0}",
            "{}",
            "{date:%Y}",
            "{time!r}",
            "{FIRST_NAME}",
            "{ first_name }",
            "{unknown}",
            "{settings.SECRET_KEY}",
            "{",
            "}",
            "{first_name",
            "%(first_name)s",
        ]
        for text in nasty:
            with self.subTest(text=text):
                self.assertEqual(render_message(text, CONTEXT), text)

    def test_mixed_text_never_raises(self):
        body = "{first_name} {{ {x.__class__} {first_name[0]} } { {date} %s %d"
        self.assertEqual(render_message(body, CONTEXT), "Ali {{ {x.__class__} {first_name[0]} } { Wed 7 Oct %s %d")

    def test_missing_or_empty_values(self):
        self.assertEqual(render_message("", CONTEXT), "")
        self.assertEqual(render_message(None, CONTEXT), "")
        self.assertEqual(render_message("[{time}]", {"time": None}), "[]")
        self.assertEqual(render_message("{date}", {}), "{date}")  # no value: left as typed

    def test_unknown_placeholders_are_reported_for_the_editor(self):
        self.assertEqual(unknown_placeholders("Hi {first_name} {patient} {x.__class__} {patient}"),
                         ["{patient}", "{x.__class__}"])
        for body in DEFAULT_TEMPLATES.values():
            self.assertEqual(unknown_placeholders(body), [])


class DateTimeFormatTests(SimpleTestCase):
    def test_dates_are_short_and_clear(self):
        self.assertEqual(format_date(date(2026, 10, 7)), "Wed 7 Oct")
        self.assertEqual(format_date(date(2026, 12, 25)), "Fri 25 Dec")

    def test_times_use_am_pm(self):
        self.assertEqual(format_time(time(15, 30)), "3:30 pm")
        self.assertEqual(format_time(time(9, 5)), "9:05 am")
        self.assertEqual(format_time(time(0, 15)), "12:15 am")
        self.assertEqual(format_time(time(12, 0)), "12:00 pm")


class DefaultTemplateTests(SimpleTestCase):
    def test_every_kind_has_a_default(self):
        self.assertEqual(set(DEFAULT_TEMPLATES), set(ReminderKind.values))

    def test_appointment_default_has_the_confirm_link_and_clinic_phone(self):
        body = DEFAULT_TEMPLATES[ReminderKind.APPOINTMENT]
        self.assertIn("Tap to confirm or ask for another time: {confirm_link}", body)
        for name in ("{clinic_name}", "{doctor_name}", "{date}", "{time}", "{clinic_phone}"):
            self.assertIn(name, body)

    def test_follow_up_and_overdue_defaults(self):
        self.assertIn("around {date}", DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])
        self.assertIn("{clinic_phone}", DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])
        self.assertIn("was due on {date}", DEFAULT_TEMPLATES[ReminderKind.OVERDUE])
        self.assertIn("{clinic_phone}", DEFAULT_TEMPLATES[ReminderKind.OVERDUE])
        self.assertEqual(DEFAULT_TEMPLATES[ReminderKind.CUSTOM], "Hello {first_name}, ")

    def test_placeholder_help_lists_exactly_the_supported_names(self):
        self.assertEqual(
            set(PLACEHOLDERS),
            {"patient_name", "first_name", "clinic_name", "clinic_phone", "doctor_name", "date", "time", "confirm_link"},
        )


class TemplateBodyTests(ReminderTestCase):
    def test_default_when_the_clinic_has_none(self):
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP), DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])

    def test_clinics_own_wording(self):
        MessageTemplate.objects.create(clinic=self.clinic, kind=ReminderKind.FOLLOW_UP, body="Come back {date}")
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP), "Come back {date}")

    def test_blank_wording_falls_back_to_default(self):
        MessageTemplate.objects.create(clinic=self.clinic, kind=ReminderKind.FOLLOW_UP, body="   ")
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP), DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])

    def test_other_clinics_and_other_languages_are_not_used(self):
        MessageTemplate.objects.create(clinic=self.other_clinic, kind=ReminderKind.FOLLOW_UP, body="Other clinic")
        MessageTemplate.objects.create(clinic=self.clinic, kind=ReminderKind.FOLLOW_UP, language="ur", body="اردو")
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP), DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP, language="ur"), "اردو")

    def test_custom_message_starts_with_the_first_name(self):
        builder = services.MessageBuilder(self.clinic)
        self.assertEqual(builder.custom_message(self.patient), "Hello Ali, ")

    def test_sample_context_reads_but_never_writes(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            sample = services.sample_context(self.clinic, ReminderKind.APPOINTMENT)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        self.assertEqual(set(sample), set(PLACEHOLDERS))
        self.assertTrue(sample["confirm_link"])


class ChannelTests(ReminderTestCase):
    def test_link_opens_the_patients_chat_with_the_message(self):
        message = "Salam Ali,\nآپ کی اپائنٹمنٹ کل ہے۔ Reply 1 & 2? 100% sure #3"
        reminder = self.make_reminder(message=message)

        url = WhatsAppLinkChannel().url_for(reminder)

        self.assertEqual(url, "https://wa.me/923001234567?text=" + quote(message))
        self.assertEqual(url, whatsapp_link("923001234567", message))
        text = url.split("?text=", 1)[1]
        self.assertIn("%0A", text)  # newline
        self.assertIn(quote("آپ"), text)  # Urdu as UTF-8 percent-encoding
        for raw in ("\n", " ", "&", "#", "?"):  # would break or cut the link if not encoded
            self.assertNotIn(raw, text)
        self.assertEqual(unquote(text), message)

    def test_no_link_without_a_whatsapp_number(self):
        patient = self.make_patient(full_name="No Number", phone="123")
        self.assertIsNone(WhatsAppLinkChannel().url_for(self.make_reminder(patient=patient)))

    def test_one_tap_is_the_channel_in_use(self):
        self.assertIsInstance(get_channel(), WhatsAppLinkChannel)

    def test_cloud_api_is_a_documented_stub(self):
        channel = WhatsAppCloudAPIChannel()
        self.assertIn("Meta", WhatsAppCloudAPIChannel.__doc__)
        with self.assertRaises(NotImplementedError):
            channel.send(self.make_reminder())
