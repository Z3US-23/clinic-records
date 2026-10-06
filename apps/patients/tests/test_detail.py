from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.appointments.models import Appointment
from apps.clinical.models import LabResult, PrescriptionItem
from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase, make_patient
from apps.patients.services import history_timeline
from apps.reminders.models import Reminder

# Clinical words used in the fixtures below. None of them may reach a receptionist's page.
CLINICAL_STRINGS = [
    "Penicillin",  # allergy
    "Diabetes type 2",  # chronic condition
    "Blood group",
    "Clinical summary",
    "Severe headache",  # presenting complaint
    "Migraine",  # diagnosis
    "Tab. Sumatriptan",  # prescription
    "HbA1c",  # lab test
    "Latest vitals",
    "Print prescription",
]


class PatientDetailTests(ClinicTestCase):
    def setUp(self):
        self.patient = self.make_patient(
            full_name="Ayesha Khan",
            guardian_name="Imran Khan",
            allergies="Penicillin",
            chronic_conditions="Diabetes type 2",
            blood_group="B+",
            city="Lahore",
        )
        self.visit = self.make_visit(
            self.patient,
            chief_complaint="Severe headache",
            diagnosis="Migraine",
            bp_systolic=130,
            bp_diastolic=85,
            pulse=78,
            temperature_c=Decimal("37.2"),
            visit_date=timezone.now() - timedelta(days=5),
        )
        PrescriptionItem.objects.create(visit=self.visit, medicine="Tab. Sumatriptan 50mg", frequency="When needed")
        self.lab = LabResult.objects.create(
            clinic=self.clinic, patient=self.patient, test_name="HbA1c", result_text="7.9 %", is_abnormal=True
        )
        self.url = reverse("patients:detail", args=[self.patient.pk])

    def test_clinician_sees_full_medical_history(self):
        self.login(self.doctor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tab"], "history")
        for text in ["Penicillin", "Diabetes type 2", "B+", "Severe headache", "Migraine", "HbA1c", "Abnormal",
                     "BP 130/85", "Pulse 78", "Temp 37.2 °C", "1 medicine", "Tab. Sumatriptan 50mg"]:
            self.assertContains(response, text)
        self.assertContains(response, reverse("clinical:visit_detail", args=[self.visit.pk]))
        self.assertContains(response, reverse("clinical:prescription_print", args=[self.visit.pk]))
        self.assertContains(response, reverse("clinical:visit_create", args=[self.patient.pk]))
        self.assertContains(response, reverse("clinical:lab_create", args=[self.patient.pk]))
        self.assertContains(response, reverse("patients:archive", args=[self.patient.pk]))

    def test_header_card(self):
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertContains(response, "Ayesha Khan")
        self.assertContains(response, self.patient.mrn)
        self.assertContains(response, "Imran Khan")
        self.assertContains(response, 'href="tel:+923001234567"')
        self.assertContains(response, "+92 300 1234567")
        self.assertContains(response, "Lahore")
        self.assertContains(response, f"{reverse('appointments:create')}?patient={self.patient.pk}")
        self.assertContains(response, f"{reverse('reminders:create')}?patient={self.patient.pk}")
        self.assertContains(response, reverse("patients:update", args=[self.patient.pk]))
        self.assertNotContains(response, "Reminders off")

    def test_receptionist_gets_no_clinical_data(self):
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=2), reason="Check-up")
        self.login(self.receptionist)
        for tab in ["", "history", "appointments", "labs", "messages", "bogus"]:
            with self.subTest(tab=tab):
                response = self.client.get(self.url, {"tab": tab} if tab else {})
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.context["tab"], ["appointments", "messages"])
                self.assertNotIn("summary", response.context)
                self.assertNotIn("timeline", response.context)
                self.assertNotIn("lab_results", response.context)
                for text in CLINICAL_STRINGS:
                    self.assertNotContains(response, text)
                self.assertNotContains(response, reverse("clinical:visit_create", args=[self.patient.pk]))
                self.assertNotContains(response, reverse("clinical:lab_create", args=[self.patient.pk]))
                self.assertNotContains(response, reverse("patients:archive", args=[self.patient.pk]))

    def test_receptionist_default_tab_is_appointments(self):
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=2), reason="Check-up")
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertEqual(response.context["tab"], "appointments")
        self.assertContains(response, "Check-up")

    def test_viewing_is_audited_without_clinical_details(self):
        self.login(self.doctor)
        self.client.get(self.url)
        log = AuditLog.objects.get(action=AuditLog.Action.VIEW)
        self.assertEqual(log.summary, f"Viewed patient {self.patient.mrn}")
        self.assertEqual(log.object_type, "Patient")
        self.assertEqual(log.object_id, str(self.patient.pk))
        self.assertEqual(log.user, self.doctor)

    def test_other_clinic_patient_is_404(self):
        other = make_patient(self.other_clinic)
        self.login(self.doctor)
        response = self.client.get(reverse("patients:detail", args=[other.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.VIEW).exists())

    def test_every_tab_renders_for_clinicians(self):
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=2))
        Reminder.objects.create(
            clinic=self.clinic, patient=self.patient, kind=Reminder.Kind.CUSTOM,
            due_date=timezone.localdate(), message="Please bring your old reports.",
        )
        self.login(self.owner)
        for tab in ["history", "appointments", "labs", "messages"]:
            with self.subTest(tab=tab):
                response = self.client.get(self.url, {"tab": tab})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["tab"], tab)
        self.assertContains(self.client.get(self.url, {"tab": "messages"}), "Please bring your old reports.")
        self.assertContains(self.client.get(self.url, {"tab": "labs"}), "7.9 %")

    def test_messages_tab_for_receptionist(self):
        reminder = Reminder.objects.create(
            clinic=self.clinic, patient=self.patient, kind=Reminder.Kind.APPOINTMENT,
            due_date=timezone.localdate(), message="See you tomorrow at 10 am.",
            status=Reminder.Status.SENT, sent_at=timezone.now(), sent_by=self.receptionist,
        )
        self.login(self.receptionist)
        response = self.client.get(self.url, {"tab": "messages"})
        self.assertContains(response, "See you tomorrow at 10 am.")
        self.assertContains(response, reminder.get_kind_display())
        self.assertContains(response, "Sent")

    def test_appointments_tab_splits_upcoming_and_past(self):
        upcoming = self.make_appointment(self.patient, when=timezone.now() + timedelta(days=2), reason="Upcoming one")
        past = self.make_appointment(
            self.patient, when=timezone.now() - timedelta(days=20), reason="Old one", status=Appointment.Status.NO_SHOW
        )
        cancelled = self.make_appointment(
            self.patient, when=timezone.now() + timedelta(days=4), status=Appointment.Status.CANCELLED
        )
        self.login(self.receptionist)
        response = self.client.get(self.url, {"tab": "appointments"})
        self.assertEqual(response.context["upcoming_appointments"], [upcoming])
        self.assertEqual(set(response.context["past_appointments"]), {past, cancelled})
        self.assertContains(response, "Did not come")
        self.assertContains(response, reverse("appointments:update", args=[upcoming.pk]))
        self.assertContains(response, "Dr. Bilal Hussain")

    def test_reminders_off_badge(self):
        self.patient.reminders_opt_in = False
        self.patient.save()
        self.login(self.receptionist)
        self.assertContains(self.client.get(self.url), "Reminders off")

    def test_archived_badge_and_restore_button(self):
        self.patient.is_archived = True
        self.patient.save()
        self.login(self.doctor)
        response = self.client.get(self.url)
        self.assertContains(response, "Archived")
        self.assertContains(response, "Restore")

    def test_archived_patient_has_no_book_or_message_buttons(self):
        """Booking and messaging pages refuse archived patients, so don't offer links that would 404."""
        self.patient.is_archived = True
        self.patient.save()
        book_url = f"{reverse('appointments:create')}?patient={self.patient.pk}"
        message_url = f"{reverse('reminders:create')}?patient={self.patient.pk}"
        for user in (self.doctor, self.receptionist):
            for tab in ("appointments", "messages"):
                with self.subTest(user=user, tab=tab):
                    self.login(user)
                    response = self.client.get(self.url, {"tab": tab})
                    self.assertNotContains(response, book_url)
                    self.assertNotContains(response, message_url)
        self.login(self.receptionist)
        self.assertContains(self.client.get(self.url), "A doctor can restore them")

    def test_no_whatsapp_number_hides_message_button_and_shows_reminders_off(self):
        self.patient.phone = "12"  # not a usable number
        self.patient.save()
        self.login(self.receptionist)
        response = self.client.get(self.url)
        self.assertContains(response, "Reminders off")
        self.assertContains(response, "No valid number")
        self.assertNotContains(response, f"{reverse('reminders:create')}?patient={self.patient.pk}")

    def test_estimated_age_marked_approx(self):
        self.patient.dob_is_estimated = True
        self.patient.save()
        self.login(self.receptionist)
        self.assertContains(self.client.get(self.url), "(approx.)")

    def test_no_known_allergies(self):
        self.patient.allergies = ""
        self.patient.save()
        self.login(self.doctor)
        self.assertContains(self.client.get(self.url), "No known allergies")


class ClinicalSummaryTests(ClinicTestCase):
    def setUp(self):
        self.patient = self.make_patient()
        self.url = reverse("patients:detail", args=[self.patient.pk])
        self.login(self.doctor)

    def summary(self):
        return self.client.get(self.url).context["summary"]

    def test_latest_vitals_come_from_latest_visit_with_any_vital(self):
        with_vitals = self.make_visit(self.patient, pulse=88, spo2=97, visit_date=timezone.now() - timedelta(days=10))
        self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=1))  # no vitals
        summary = self.summary()
        self.assertEqual(summary["vitals_visit"], with_vitals)
        self.assertEqual(summary["vitals"], ["Pulse 88", "SpO₂ 97%"])

    def test_current_medicines_from_latest_visit_with_any(self):
        older = self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=10))
        PrescriptionItem.objects.create(visit=older, medicine="Tab. Metformin 500mg")
        self.make_visit(self.patient, visit_date=timezone.now() - timedelta(days=1))
        summary = self.summary()
        self.assertEqual(summary["medicines_visit"], older)
        self.assertEqual([m.medicine for m in summary["medicines"]], ["Tab. Metformin 500mg"])

    def test_follow_up_overdue(self):
        self.make_visit(
            self.patient, visit_date=timezone.now() - timedelta(days=20),
            follow_up_date=timezone.localdate() - timedelta(days=3),
        )
        self.assertEqual(self.summary()["follow_up"]["state"], "overdue")
        self.assertContains(self.client.get(self.url), "Overdue")

    def test_follow_up_booked_when_later_appointment_exists(self):
        self.make_visit(
            self.patient, visit_date=timezone.now() - timedelta(days=20),
            follow_up_date=timezone.localdate() - timedelta(days=3),
        )
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=1))
        self.assertEqual(self.summary()["follow_up"]["state"], "booked")

    def test_cancelled_appointment_does_not_count_as_booked(self):
        self.make_visit(
            self.patient, visit_date=timezone.now() - timedelta(days=20),
            follow_up_date=timezone.localdate() - timedelta(days=3),
        )
        self.make_appointment(self.patient, when=timezone.now() + timedelta(days=1), status=Appointment.Status.CANCELLED)
        self.assertEqual(self.summary()["follow_up"]["state"], "overdue")

    def test_the_visits_own_appointment_does_not_count_as_coming_back(self):
        # Patient was seen a few minutes before their booked slot; the follow-up is now overdue.
        slot = timezone.now() - timedelta(days=20)
        own = self.make_appointment(self.patient, when=slot, status=Appointment.Status.COMPLETED)
        self.make_visit(
            self.patient, appointment=own, visit_date=slot - timedelta(minutes=10),
            follow_up_date=timezone.localdate() - timedelta(days=3),
        )
        self.assertEqual(self.summary()["follow_up"]["state"], "overdue")

    def test_follow_up_due(self):
        self.make_visit(self.patient, follow_up_date=timezone.localdate() + timedelta(days=7))
        self.assertEqual(self.summary()["follow_up"]["state"], "due")

    def test_no_follow_up(self):
        self.assertIsNone(self.summary()["follow_up"])
        self.make_visit(self.patient)
        self.assertIsNone(self.summary()["follow_up"])


class HistoryTimelineTests(ClinicTestCase):
    def test_merges_newest_first_and_skips_appointments_with_a_visit(self):
        patient = self.make_patient()
        now = timezone.now()
        seen_appointment = self.make_appointment(patient, when=now - timedelta(days=3), status=Appointment.Status.COMPLETED)
        visit = self.make_visit(patient, appointment=seen_appointment, visit_date=now - timedelta(days=3))
        missed = self.make_appointment(patient, when=now - timedelta(days=10), status=Appointment.Status.NO_SHOW)
        lab = LabResult.objects.create(
            clinic=self.clinic, patient=patient, test_name="CBC", result_date=timezone.localdate() - timedelta(days=1)
        )
        self.make_appointment(patient, when=now + timedelta(days=3))  # future: not history

        entries, total = history_timeline(patient, {})
        self.assertEqual([(e["kind"], e["obj"]) for e in entries], [("lab", lab), ("visit", visit), ("appointment", missed)])
        self.assertEqual(total, 3)

    def test_cap(self):
        patient = self.make_patient()
        for days in range(4):
            self.make_visit(patient, visit_date=timezone.now() - timedelta(days=days))
        entries, total = history_timeline(patient, {}, limit=2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(total, 4)

    def test_cap_note_on_page(self):
        patient = self.make_patient()
        for days in range(101):
            self.make_visit(patient, visit_date=timezone.now() - timedelta(days=days))
        self.login(self.doctor)
        response = self.client.get(reverse("patients:detail", args=[patient.pk]))
        self.assertEqual(len(response.context["timeline"]), 100)
        self.assertContains(response, "Showing the latest 100 of 101 entries")


class ArchiveTests(ClinicTestCase):
    def setUp(self):
        self.patient = self.make_patient(full_name="Ali Raza")
        self.url = reverse("patients:archive", args=[self.patient.pk])

    def test_get_not_allowed(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.is_archived)

    def test_receptionist_forbidden(self):
        self.login(self.receptionist)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.is_archived)

    def test_archive_and_restore(self):
        for user in (self.doctor, self.owner):
            with self.subTest(user=user):
                self.login(user)
                response = self.client.post(self.url)
                self.assertRedirects(response, self.patient.get_absolute_url(), fetch_redirect_response=False)
                self.patient.refresh_from_db()
                self.assertTrue(self.patient.is_archived)

                self.client.post(self.url)
                self.patient.refresh_from_db()
                self.assertFalse(self.patient.is_archived)

        summaries = list(
            AuditLog.objects.filter(action=AuditLog.Action.UPDATE).order_by("pk").values_list("summary", flat=True)
        )
        mrn = self.patient.mrn
        self.assertEqual(summaries, [f"Archived patient {mrn}", f"Restored patient {mrn}"] * 2)

    def test_archived_patient_leaves_the_list(self):
        self.login(self.doctor)
        self.client.post(self.url)
        # (The page's flash message still names the patient, so check the table rows.)
        response = self.client.get(reverse("patients:list"))
        self.assertEqual(list(response.context["patients"]), [])
        response = self.client.get(reverse("patients:list"), {"archived": "1"})
        self.assertEqual(list(response.context["patients"]), [self.patient])
        search = self.client.get(reverse("patients:search_json"), {"q": "Ali Raza"}).json()
        self.assertEqual(search, {"results": []})

    def test_other_clinic_is_404(self):
        other = make_patient(self.other_clinic)
        self.login(self.doctor)
        response = self.client.post(reverse("patients:archive", args=[other.pk]))
        self.assertEqual(response.status_code, 404)
        other.refresh_from_db()
        self.assertFalse(other.is_archived)

    def test_sign_in_required(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
