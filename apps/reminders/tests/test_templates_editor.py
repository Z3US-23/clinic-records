"""Message templates editor (owner only)."""

from django.contrib.messages import get_messages
from django.urls import reverse

from apps.core.models import AuditLog
from apps.reminders.models import MessageTemplate, ReminderKind
from apps.reminders.services import DEFAULT_TEMPLATES, get_template_body

from .base import ReminderTestCase

URL = reverse("reminders:templates")


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class TemplatesAccessTests(ReminderTestCase):
    def test_owner_sees_all_four_templates_with_placeholders_and_previews(self):
        self.login(self.owner)
        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([card["kind"] for card in response.context["cards"]], list(ReminderKind.values))
        for name in ("{confirm_link}", "{first_name}", "{clinic_phone}"):
            self.assertContains(response, name)
        self.assertContains(response, "Link the patient taps to confirm")
        # Preview with made-up details, not a real patient.
        self.assertContains(response, "Hello Ayesha, this is a reminder of your appointment at Al-Noor Family Clinic")
        self.assertContains(response, "Reminder timing")

    def test_doctor_and_receptionist_are_refused(self):
        for user in (self.doctor, self.receptionist):
            with self.subTest(role=user.full_name):
                self.login(user)
                self.assertEqual(self.client.get(URL).status_code, 403)
                response = self.client.post(URL, {"kind": ReminderKind.FOLLOW_UP, "body": "Hacked", "action": "save"})
                self.assertEqual(response.status_code, 403)
        self.assertFalse(MessageTemplate.objects.exists())

    def test_signed_out_goes_to_login(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_other_clinics_wording_is_not_shown(self):
        MessageTemplate.objects.create(clinic=self.other_clinic, kind=ReminderKind.FOLLOW_UP, body="Other clinic words")
        self.login(self.owner)
        self.assertNotContains(self.client.get(URL), "Other clinic words")


class TemplatesEditTests(ReminderTestCase):
    def setUp(self):
        super().setUp()
        self.login(self.owner)

    def post(self, kind, body, action="save"):
        return self.client.post(URL, {"kind": kind, "body": body, "action": action})

    def test_save_stores_the_wording_and_audits(self):
        response = self.post(ReminderKind.FOLLOW_UP, "Salam {first_name}, please come back around {date}.")

        self.assertRedirects(response, URL + "#template-follow_up", fetch_redirect_response=False)
        template = MessageTemplate.objects.get(clinic=self.clinic, kind=ReminderKind.FOLLOW_UP, language="en")
        self.assertEqual(template.body, "Salam {first_name}, please come back around {date}.")
        self.assertEqual(get_template_body(self.clinic, ReminderKind.FOLLOW_UP), template.body)
        entry = AuditLog.objects.get(action=AuditLog.Action.UPDATE, clinic=self.clinic)
        self.assertEqual(entry.summary, "Updated follow-up message template")
        self.assertEqual(entry.user, self.owner)

    def test_saving_again_updates_the_same_row(self):
        self.post(ReminderKind.OVERDUE, "First {date}")
        self.post(ReminderKind.OVERDUE, "Second {date}")
        self.assertEqual(MessageTemplate.objects.get(kind=ReminderKind.OVERDUE).body, "Second {date}")

    def test_saving_the_standard_wording_stores_nothing(self):
        self.post(ReminderKind.FOLLOW_UP, "Changed")
        self.post(ReminderKind.FOLLOW_UP, DEFAULT_TEMPLATES[ReminderKind.FOLLOW_UP])
        self.assertFalse(MessageTemplate.objects.exists())

    def test_body_longer_than_1000_characters_is_refused(self):
        response = self.post(ReminderKind.FOLLOW_UP, "x" * 1001)
        self.assertEqual(response.status_code, 200)
        card = next(c for c in response.context["cards"] if c["kind"] == ReminderKind.FOLLOW_UP)
        self.assertIn("body", card["form"].errors)
        self.assertFalse(MessageTemplate.objects.exists())
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.UPDATE).exists())

        self.post(ReminderKind.FOLLOW_UP, "x" * 1000)
        self.assertTrue(MessageTemplate.objects.exists())

    def test_empty_body_is_refused(self):
        response = self.post(ReminderKind.CUSTOM, "")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MessageTemplate.objects.exists())

    def test_missing_confirm_link_warns_but_saves(self):
        response = self.post(ReminderKind.APPOINTMENT, "See you {date} at {time}.")
        self.assertTrue(MessageTemplate.objects.filter(kind=ReminderKind.APPOINTMENT).exists())
        self.assertIn("{confirm_link}", " ".join(message_texts(response)))

        page = self.client.get(URL)
        card = next(c for c in page.context["cards"] if c["kind"] == ReminderKind.APPOINTMENT)
        self.assertTrue(any("{confirm_link}" in w for w in card["warnings"]))

    def test_unknown_placeholders_warn(self):
        response = self.post(ReminderKind.FOLLOW_UP, "Hi {patient}")
        self.assertIn("{patient}", " ".join(message_texts(response)))

    def test_preview_writes_nothing(self):
        audit_before = AuditLog.objects.count()

        response = self.post(ReminderKind.FOLLOW_UP, "Preview for {first_name} on {date}", action="preview")

        self.assertEqual(response.status_code, 200)
        card = next(c for c in response.context["cards"] if c["kind"] == ReminderKind.FOLLOW_UP)
        self.assertTrue(card["preview"].startswith("Preview for Ayesha on "))
        self.assertFalse(MessageTemplate.objects.exists())
        self.assertEqual(AuditLog.objects.count(), audit_before)

    def test_preview_never_evaluates_malicious_placeholders_and_is_escaped(self):
        body = "{first_name.__class__} {first_name[0]} <b>{first_name}</b>"
        response = self.post(ReminderKind.CUSTOM, body, action="preview")
        card = next(c for c in response.context["cards"] if c["kind"] == ReminderKind.CUSTOM)
        self.assertEqual(card["preview"], "{first_name.__class__} {first_name[0]} <b>Ayesha</b>")
        self.assertContains(response, "&lt;b&gt;Ayesha&lt;/b&gt;")
        self.assertNotContains(response, "<b>Ayesha</b>")

    def test_reset_to_default(self):
        self.post(ReminderKind.OVERDUE, "My own words {date}")

        response = self.post(ReminderKind.OVERDUE, "", action="reset")

        self.assertRedirects(response, URL + "#template-overdue", fetch_redirect_response=False)
        self.assertFalse(MessageTemplate.objects.exists())
        self.assertEqual(get_template_body(self.clinic, ReminderKind.OVERDUE), DEFAULT_TEMPLATES[ReminderKind.OVERDUE])
        self.assertTrue(AuditLog.objects.filter(summary="Reset missed follow-up message template to default").exists())

    def test_reset_only_touches_this_clinic(self):
        MessageTemplate.objects.create(clinic=self.other_clinic, kind=ReminderKind.OVERDUE, body="Other clinic")
        self.post(ReminderKind.OVERDUE, "", action="reset")
        self.assertTrue(MessageTemplate.objects.filter(clinic=self.other_clinic).exists())

    def test_saving_never_changes_another_clinic(self):
        MessageTemplate.objects.create(clinic=self.other_clinic, kind=ReminderKind.FOLLOW_UP, body="Other clinic")
        self.post(ReminderKind.FOLLOW_UP, "Mine {date}")
        self.assertEqual(MessageTemplate.objects.get(clinic=self.other_clinic).body, "Other clinic")
        self.assertEqual(MessageTemplate.objects.get(clinic=self.clinic).body, "Mine {date}")

    def test_unknown_kind_is_refused(self):
        response = self.post("nonsense", "Hello")
        self.assertRedirects(response, URL, fetch_redirect_response=False)
        self.assertFalse(MessageTemplate.objects.exists())
