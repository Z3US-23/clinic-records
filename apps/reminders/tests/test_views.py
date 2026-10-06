"""Reminder screens: list, one-tap send, skip, edit and custom messages."""

from datetime import timedelta
from urllib.parse import quote, unquote

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.testing import make_appointment, make_patient
from apps.reminders.models import Reminder, ReminderKind

from .base import ReminderTestCase

PENDING, SENT, SKIPPED = Reminder.Status.PENDING, Reminder.Status.SENT, Reminder.Status.SKIPPED
WA_PREFIX = "https://wa.me/923001234567?text="


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class ReminderViewTestCase(ReminderTestCase):
    def setUp(self):
        super().setUp()
        self.other_patient = make_patient(self.other_clinic, full_name="Other Clinic Patient")
        self.other_reminder = Reminder.objects.create(
            clinic=self.other_clinic, patient=self.other_patient, kind=ReminderKind.CUSTOM,
            due_date=self.today, message="Other clinic secret message",
        )

    @property
    def all_staff(self):
        return (self.owner, self.doctor, self.receptionist)


# --- List ------------------------------------------------------------------------------


class ReminderListTests(ReminderViewTestCase):
    url = reverse("reminders:list")

    def test_every_role_can_open_it(self):
        for user in self.all_staff:
            with self.subTest(role=user.full_name):
                self.login(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "reminders/list.html")
                self.assertContains(response, "How sending works")

    def test_signed_out_goes_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_opening_the_page_prepares_due_reminders(self):
        make_appointment(self.patient, self.doctor, self.tomorrow_evening())
        self.login(self.receptionist)

        response = self.client.get(self.url)

        reminder = Reminder.objects.get(clinic=self.clinic, kind=ReminderKind.APPOINTMENT)
        self.assertContains(response, reverse("reminders:send", args=[reminder.pk]))
        self.assertContains(response, "Send on WhatsApp")
        self.assertContains(response, "btn-whatsapp")

    def test_tabs_show_the_right_reminders_with_counts(self):
        due_today = self.make_reminder(message="Due today message")
        late = self.make_reminder(message="Late message", due_date=self.day(-3))
        upcoming = self.make_reminder(message="Upcoming message", due_date=self.day(4))
        sent = self.make_reminder(message="Sent message", status=SENT, sent_at=timezone.now(), sent_by=self.receptionist)
        old_sent = self.make_reminder(message="Old sent message", status=SENT,
                                      sent_at=timezone.now() - timedelta(days=45))
        skipped = self.make_reminder(message="Skipped message", status=SKIPPED)
        self.login(self.doctor)

        response = self.client.get(self.url)
        counts = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
        self.assertEqual(counts, {"due": 2, "upcoming": 1, "sent": 1, "skipped": 1})
        self.assertEqual(response.context["current_tab"], "due")
        self.assertEqual({r.pk for r in response.context["reminders"]}, {due_today.pk, late.pk})
        self.assertContains(response, "3 days late")

        tabs = {"upcoming": {upcoming.pk}, "sent": {sent.pk}, "skipped": {skipped.pk}}
        for tab, expected in tabs.items():
            with self.subTest(tab=tab):
                response = self.client.get(self.url, {"tab": tab})
                self.assertEqual(response.status_code, 200)
                self.assertEqual({r.pk for r in response.context["reminders"]}, expected)
        self.assertNotIn(old_sent.pk, tabs["sent"])

    def test_sent_tab_shows_who_sent_it_and_send_again(self):
        sent = self.make_reminder(status=SENT, sent_at=timezone.now(), sent_by=self.receptionist)
        self.login(self.owner)
        response = self.client.get(self.url, {"tab": "sent"})
        self.assertContains(response, "by Hina Malik")
        self.assertContains(response, "Send again")
        self.assertContains(response, reverse("reminders:send", args=[sent.pk]))

    def test_unknown_tab_falls_back_to_to_send(self):
        self.login(self.owner)
        response = self.client.get(self.url, {"tab": "nonsense"})
        self.assertEqual(response.context["current_tab"], "due")

    def test_patient_without_whatsapp_gets_call_and_fix_number_instead(self):
        no_number = self.make_patient(full_name="Kamran Shah", phone="12345")
        reminder = self.make_reminder(patient=no_number)
        self.login(self.receptionist)

        response = self.client.get(self.url)

        self.assertContains(response, "No WhatsApp number")
        self.assertContains(response, 'href="tel:12345"')
        self.assertContains(response, reverse("patients:update", args=[no_number.pk]))
        self.assertNotContains(response, reverse("reminders:send", args=[reminder.pk]))

    def test_rows_link_to_the_patient_and_show_the_phone(self):
        self.make_reminder()
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertContains(response, reverse("patients:detail", args=[self.patient.pk]))
        self.assertContains(response, "+92 300 1234567")

    def test_messages_are_escaped(self):
        self.make_reminder(message="<script>alert('x')</script>")
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertNotContains(response, "<script>alert(")
        self.assertContains(response, "&lt;script&gt;")

    def test_other_clinics_reminders_are_never_shown(self):
        self.login(self.owner)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Other clinic secret message")
        self.assertNotContains(response, "Other Clinic Patient")
        self.assertEqual(sum(tab["count"] for tab in response.context["tabs"]), 0)

    def test_templates_link_only_for_owner(self):
        self.login(self.receptionist)
        self.assertNotContains(self.client.get(self.url), reverse("reminders:templates"))
        self.login(self.owner)
        self.assertContains(self.client.get(self.url), reverse("reminders:templates"))


# --- Send ------------------------------------------------------------------------------


class SendReminderTests(ReminderViewTestCase):
    def send(self, reminder, **data):
        return self.client.post(reverse("reminders:send", args=[reminder.pk]), data)

    def test_send_marks_sent_audits_and_opens_whatsapp(self):
        reminder = self.make_reminder(message="Hello Ali, see you tomorrow.")
        self.login(self.receptionist)

        response = self.send(reminder)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], WA_PREFIX + quote("Hello Ali, see you tomorrow."))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, SENT)
        self.assertEqual(reminder.sent_by, self.receptionist)
        self.assertIsNotNone(reminder.sent_at)
        entry = self.audit_entries(reminder, AuditLog.Action.SEND).get()
        self.assertEqual(entry.summary, f"Sent custom reminder to {self.patient.mrn}")
        self.assertEqual(entry.user, self.receptionist)
        self.assertEqual(entry.clinic, self.clinic)

    def test_every_role_can_send(self):
        for user in self.all_staff:
            with self.subTest(role=user.full_name):
                reminder = self.make_reminder()
                self.login(user)
                self.assertTrue(self.send(reminder)["Location"].startswith(WA_PREFIX))

    def test_appointment_reminder_audit_wording(self):
        appointment = make_appointment(self.patient, self.doctor, self.tomorrow_evening())
        reminder = self.make_reminder(kind=ReminderKind.APPOINTMENT, appointment=appointment)
        self.login(self.doctor)
        self.send(reminder)
        self.assertEqual(
            self.audit_entries(reminder, AuditLog.Action.SEND).get().summary,
            f"Sent appointment reminder to {self.patient.mrn}",
        )

    def test_newlines_and_urdu_are_url_encoded(self):
        message = "السلام علیکم Ali,\nکل آپ کی اپائنٹمنٹ ہے۔\n\nTap: https://x.test/c/a?b=1&c=2"
        reminder = self.make_reminder(message=message)
        self.login(self.receptionist)

        location = self.send(reminder)["Location"]

        self.assertTrue(location.startswith(WA_PREFIX))
        text = location[len(WA_PREFIX):]
        self.assertIn("%0A%0A", text)
        self.assertIn(quote("السلام"), text)
        self.assertNotIn("&", text)
        self.assertNotIn("\n", text)
        self.assertEqual(unquote(text), message)

    def test_redirect_is_only_ever_the_whatsapp_link(self):
        reminder = self.make_reminder()
        self.login(self.receptionist)
        response = self.send(reminder, next="https://evil.example/")
        self.assertTrue(response["Location"].startswith(WA_PREFIX))

    def test_get_is_not_allowed(self):
        reminder = self.make_reminder()
        self.login(self.receptionist)
        response = self.client.get(reverse("reminders:send", args=[reminder.pk]))
        self.assertEqual(response.status_code, 405)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, PENDING)

    def test_other_clinics_reminder_is_404(self):
        self.login(self.owner)
        self.assertEqual(self.send(self.other_reminder).status_code, 404)
        self.other_reminder.refresh_from_db()
        self.assertEqual(self.other_reminder.status, PENDING)

    def test_no_whatsapp_number_explains_and_goes_back(self):
        no_number = self.make_patient(full_name="Kamran Shah", phone="12345")
        reminder = self.make_reminder(patient=no_number)
        self.login(self.receptionist)

        response = self.send(reminder)

        self.assertRedirects(response, reverse("reminders:list"), fetch_redirect_response=False)
        self.assertIn("Kamran Shah has no valid WhatsApp number", " ".join(message_texts(response)))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, PENDING)
        self.assertFalse(self.audit_entries(reminder, AuditLog.Action.SEND).exists())

    def test_error_goes_back_to_a_safe_next_url_only(self):
        reminder = self.make_reminder(status=SKIPPED)
        self.login(self.receptionist)
        safe = reverse("reminders:list") + "?tab=skipped"
        self.assertRedirects(self.send(reminder, next=safe), safe, fetch_redirect_response=False)
        self.assertRedirects(self.send(reminder, next="https://evil.example/"), reverse("reminders:list"),
                             fetch_redirect_response=False)

    def test_send_again_keeps_the_original_sent_time(self):
        first_sent = timezone.now() - timedelta(days=2)
        reminder = self.make_reminder(status=SENT, sent_at=first_sent, sent_by=self.doctor)
        self.login(self.receptionist)

        response = self.send(reminder)

        self.assertTrue(response["Location"].startswith(WA_PREFIX))
        reminder.refresh_from_db()
        self.assertEqual(reminder.sent_at, first_sent)
        self.assertEqual(reminder.sent_by, self.doctor)
        entry = self.audit_entries(reminder, AuditLog.Action.SEND).get()
        self.assertEqual(entry.summary, f"Sent custom reminder to {self.patient.mrn} again")

    def test_skipped_reminder_is_not_sent(self):
        reminder = self.make_reminder(status=SKIPPED)
        self.login(self.receptionist)
        response = self.send(reminder)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith("https://wa.me"))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, SKIPPED)

    def test_archived_patient_is_not_sent(self):
        self.patient.is_archived = True
        self.patient.save()
        reminder = self.make_reminder()
        self.login(self.receptionist)
        response = self.send(reminder)
        self.assertFalse(response["Location"].startswith("https://wa.me"))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, PENDING)


# --- Skip ------------------------------------------------------------------------------


class SkipReminderTests(ReminderViewTestCase):
    def skip(self, reminder):
        return self.client.post(reverse("reminders:skip", args=[reminder.pk]))

    def test_skip_marks_skipped_and_audits(self):
        reminder = self.make_reminder()
        self.login(self.receptionist)

        response = self.skip(reminder)

        self.assertRedirects(response, reverse("reminders:list"), fetch_redirect_response=False)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, SKIPPED)
        entry = self.audit_entries(reminder, AuditLog.Action.UPDATE).get()
        self.assertEqual(entry.summary, f"Skipped custom reminder for {self.patient.mrn}")

    def test_sent_reminder_stays_sent(self):
        reminder = self.make_reminder(status=SENT, sent_at=timezone.now())
        self.login(self.receptionist)
        self.skip(reminder)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, SENT)
        self.assertFalse(self.audit_entries(reminder).exists())

    def test_get_is_not_allowed(self):
        reminder = self.make_reminder()
        self.login(self.receptionist)
        self.assertEqual(self.client.get(reverse("reminders:skip", args=[reminder.pk])).status_code, 405)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, PENDING)

    def test_other_clinics_reminder_is_404(self):
        self.login(self.owner)
        self.assertEqual(self.skip(self.other_reminder).status_code, 404)
        self.other_reminder.refresh_from_db()
        self.assertEqual(self.other_reminder.status, PENDING)


# --- Edit ------------------------------------------------------------------------------


class ReminderUpdateTests(ReminderViewTestCase):
    def url(self, reminder):
        return reverse("reminders:update", args=[reminder.pk])

    def test_every_role_can_open_a_pending_reminder(self):
        reminder = self.make_reminder(message="Original words")
        for user in self.all_staff:
            with self.subTest(role=user.full_name):
                self.login(user)
                response = self.client.get(self.url(reminder))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Original words")
                self.assertContains(response, "Save and send on WhatsApp")

    def test_edit_saves_and_audits(self):
        reminder = self.make_reminder(message="Original words")
        self.login(self.receptionist)

        response = self.client.post(self.url(reminder), {
            "message": "New words", "due_date": self.today.isoformat(), "action": "save",
        })

        self.assertRedirects(response, reverse("reminders:list") + "?tab=due", fetch_redirect_response=False)
        reminder.refresh_from_db()
        self.assertEqual(reminder.message, "New words")
        self.assertEqual(reminder.status, PENDING)
        self.assertEqual(self.audit_entries(reminder, AuditLog.Action.UPDATE).get().summary,
                         f"Edited custom reminder for {self.patient.mrn}")

    def test_save_and_send_opens_whatsapp_with_the_new_text(self):
        reminder = self.make_reminder(message="Original words")
        self.login(self.receptionist)

        response = self.client.post(self.url(reminder), {
            "message": "New words\nLine two", "due_date": self.today.isoformat(), "action": "send",
        })

        self.assertEqual(response["Location"], WA_PREFIX + quote("New words\nLine two"))
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, SENT)

    def test_moving_to_a_later_day(self):
        reminder = self.make_reminder()
        self.login(self.receptionist)
        response = self.client.post(self.url(reminder), {
            "message": reminder.message, "due_date": self.day(3).isoformat(), "action": "save",
        })
        self.assertRedirects(response, reverse("reminders:list") + "?tab=upcoming", fetch_redirect_response=False)
        reminder.refresh_from_db()
        self.assertEqual(reminder.due_date, self.day(3))

    def test_invalid_input_shows_errors(self):
        reminder = self.make_reminder(message="Original words")
        self.login(self.receptionist)
        for data in (
            {"message": "", "due_date": self.today.isoformat()},
            {"message": "x" * 2001, "due_date": self.today.isoformat()},
            {"message": "Fine", "due_date": self.day(-2).isoformat()},
        ):
            with self.subTest(data=data["due_date"]):
                response = self.client.post(self.url(reminder), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        reminder.refresh_from_db()
        self.assertEqual(reminder.message, "Original words")

    def test_sent_reminders_are_read_only(self):
        reminder = self.make_reminder(message="Already sent", status=SENT, sent_at=timezone.now())
        self.login(self.receptionist)

        response = self.client.get(self.url(reminder))
        self.assertRedirects(response, reverse("reminders:list") + "?tab=sent", fetch_redirect_response=False)
        self.assertIn("already sent", " ".join(message_texts(response)))

        self.client.post(self.url(reminder), {"message": "Changed", "due_date": self.today.isoformat()})
        reminder.refresh_from_db()
        self.assertEqual(reminder.message, "Already sent")

    def test_other_clinics_reminder_is_404(self):
        self.login(self.owner)
        self.assertEqual(self.client.get(self.url(self.other_reminder)).status_code, 404)
        response = self.client.post(self.url(self.other_reminder), {
            "message": "Hacked", "due_date": self.today.isoformat(),
        })
        self.assertEqual(response.status_code, 404)
        self.other_reminder.refresh_from_db()
        self.assertEqual(self.other_reminder.message, "Other clinic secret message")


# --- Custom message ------------------------------------------------------------------------


class CustomMessageTests(ReminderViewTestCase):
    url = reverse("reminders:create")

    def patient_url(self, patient):
        return f"{self.url}?patient={patient.pk}"

    def test_without_a_patient_shows_a_search(self):
        self.make_patient(full_name="Alia Archived", is_archived=True)
        self.login(self.receptionist)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "reminders/reminder_create_search.html")

        response = self.client.get(self.url, {"q": "Ali"})
        self.assertEqual([p.pk for p in response.context["results"]], [self.patient.pk])
        self.assertContains(response, self.patient_url(self.patient))

        response = self.client.get(self.url, {"q": "1234567"})  # by phone number
        self.assertEqual([p.pk for p in response.context["results"]], [self.patient.pk])

        response = self.client.get(self.url, {"q": "Other Clinic"})
        self.assertEqual(response.context["results"], [])

    def test_every_role_can_write_a_message(self):
        for user in self.all_staff:
            with self.subTest(role=user.full_name):
                self.login(user)
                response = self.client.get(self.patient_url(self.patient))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["form"].initial["message"], "Hello Ali, ")
                self.assertContains(response, "Send now on WhatsApp")
                self.assertContains(response, "Save to send later")

    def test_other_clinic_archived_or_bad_patient_is_404(self):
        archived = self.make_patient(full_name="Old Record", is_archived=True)
        self.login(self.owner)
        for query in (str(self.other_patient.pk), str(archived.pk), "abc", "1 OR 1=1"):
            with self.subTest(patient=query):
                self.assertEqual(self.client.get(self.url, {"patient": query}).status_code, 404)
        response = self.client.post(self.patient_url(self.other_patient), {"message": "Hi", "action": "send"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Reminder.objects.filter(kind=ReminderKind.CUSTOM, clinic=self.clinic).exists())

    def test_send_now_creates_sends_and_audits(self):
        self.login(self.receptionist)

        response = self.client.post(self.patient_url(self.patient), {
            "message": "Hello Ali, your report is ready.", "due_date": self.day(5).isoformat(), "action": "send",
        })

        self.assertEqual(response["Location"], WA_PREFIX + quote("Hello Ali, your report is ready."))
        reminder = Reminder.objects.get(clinic=self.clinic, kind=ReminderKind.CUSTOM)
        self.assertEqual(reminder.patient, self.patient)
        self.assertEqual(reminder.due_date, self.today)  # sent now, so "due" today
        self.assertEqual(reminder.status, SENT)
        self.assertEqual(reminder.created_by, self.receptionist)
        self.assertEqual(reminder.sent_by, self.receptionist)
        self.assertTrue(self.audit_entries(reminder, AuditLog.Action.CREATE).exists())
        self.assertTrue(self.audit_entries(reminder, AuditLog.Action.SEND).exists())

    def test_save_for_later(self):
        self.login(self.doctor)

        response = self.client.post(self.patient_url(self.patient), {
            "message": "Hello Ali, time for your blood test.", "due_date": self.day(5).isoformat(), "action": "save",
        })

        self.assertRedirects(response, reverse("reminders:list") + "?tab=upcoming", fetch_redirect_response=False)
        reminder = Reminder.objects.get(clinic=self.clinic, kind=ReminderKind.CUSTOM)
        self.assertEqual(reminder.status, PENDING)
        self.assertEqual(reminder.due_date, self.day(5))
        self.assertIsNone(reminder.sent_at)

    def test_save_without_a_date_is_due_today(self):
        self.login(self.doctor)
        response = self.client.post(self.patient_url(self.patient), {"message": "Hello", "action": "save"})
        self.assertRedirects(response, reverse("reminders:list") + "?tab=due", fetch_redirect_response=False)
        self.assertEqual(Reminder.objects.get(clinic=self.clinic, kind=ReminderKind.CUSTOM).due_date, self.today)

    def test_invalid_input_creates_nothing(self):
        self.login(self.doctor)
        for data in (
            {"message": "", "action": "save"},
            {"message": "Hello", "due_date": self.day(-1).isoformat(), "action": "save"},
            {"message": "x" * 2001, "action": "send"},
        ):
            with self.subTest(data=data):
                response = self.client.post(self.patient_url(self.patient), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.assertFalse(Reminder.objects.filter(clinic=self.clinic).exists())

    def test_send_now_without_a_number_creates_nothing(self):
        no_number = self.make_patient(full_name="Kamran Shah", phone="12345")
        self.login(self.receptionist)

        response = self.client.get(self.patient_url(no_number))
        self.assertNotContains(response, "Send now on WhatsApp")
        self.assertContains(response, "no valid WhatsApp number")

        response = self.client.post(self.patient_url(no_number), {"message": "Hello", "action": "send"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Reminder.objects.filter(clinic=self.clinic).exists())

    def test_create_is_login_only(self):
        response = self.client.get(self.patient_url(self.patient))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
