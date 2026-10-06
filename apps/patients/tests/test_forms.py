from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Clinic, Membership
from apps.core.models import AuditLog
from apps.core.testing import ClinicTestCase, make_clinic, make_patient, make_user
from apps.patients.forms import PatientForm, years_before
from apps.patients.models import Patient

CREATE_URL = reverse("patients:create")


def patient_data(**overrides):
    data = {
        "full_name": "Ayesha Khan",
        "guardian_name": "Imran Khan",
        "sex": "F",
        "date_of_birth": "1988-04-15",
        "age_years": "",
        "phone": "0300-1234567",
        "whatsapp_phone": "",
        "reminders_opt_in": "on",
        "city": "Lahore",
        "address": "House 12, Model Town",
        "notes": "Prefers Urdu",
        "next_action": "save",
    }
    data.update(overrides)
    return data


class PatientCreateTests(ClinicTestCase):
    def test_every_role_can_open_the_form(self):
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(user=user):
                self.login(user)
                response = self.client.get(CREATE_URL)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Personal details")
                self.assertContains(response, "Contact &amp; reminders")
                self.assertContains(response, "Save &amp; book appointment")

    def test_medical_section_only_for_clinicians(self):
        self.login(self.doctor)
        response = self.client.get(CREATE_URL)
        self.assertContains(response, "Medical (doctors only)")
        self.assertContains(response, 'name="allergies"')

        self.login(self.receptionist)
        response = self.client.get(CREATE_URL)
        self.assertNotContains(response, "Medical (doctors only)")
        self.assertNotContains(response, 'name="allergies"')
        self.assertNotContains(response, 'name="blood_group"')
        self.assertNotContains(response, 'name="chronic_conditions"')

    def test_create_patient(self):
        self.login(self.doctor)
        response = self.client.post(
            CREATE_URL, patient_data(allergies="Penicillin", blood_group="B+", chronic_conditions="Asthma")
        )
        patient = Patient.objects.get(full_name="Ayesha Khan")
        self.assertRedirects(response, patient.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(patient.clinic, self.clinic)
        self.assertEqual(patient.created_by, self.doctor)
        self.assertTrue(patient.mrn.startswith("P-"))
        self.assertEqual(patient.whatsapp_number, "923001234567")
        self.assertEqual(patient.date_of_birth, date(1988, 4, 15))
        self.assertFalse(patient.dob_is_estimated)
        self.assertEqual(patient.allergies, "Penicillin")
        self.assertEqual(patient.blood_group, "B+")

        log = AuditLog.objects.get(action=AuditLog.Action.CREATE)
        self.assertEqual(log.summary, f"Added patient {patient.mrn}")
        self.assertEqual(log.clinic, self.clinic)
        self.assertEqual(log.object_id, str(patient.pk))

    def test_receptionist_cannot_set_clinical_fields(self):
        self.login(self.receptionist)
        self.client.post(CREATE_URL, patient_data(allergies="Sneaky", blood_group="O+"))
        patient = Patient.objects.get(full_name="Ayesha Khan")
        self.assertEqual(patient.allergies, "")
        self.assertEqual(patient.blood_group, "")

    def test_save_and_book(self):
        self.login(self.receptionist)
        response = self.client.post(CREATE_URL, patient_data(next_action="book"))
        patient = Patient.objects.get(full_name="Ayesha Khan")
        self.assertRedirects(
            response, f"{reverse('appointments:create')}?patient={patient.pk}", fetch_redirect_response=False
        )

    def test_phone_is_required_and_validated(self):
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(phone=""))
        self.assertFormError(response.context["form"], "phone", "This field is required.")

        response = self.client.post(CREATE_URL, patient_data(phone="12"))
        self.assertContains(response, "e.g. 0300-1234567")
        self.assertIn("phone", response.context["form"].errors)
        self.assertFalse(Patient.objects.exists())

    def test_whatsapp_phone_validated(self):
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(whatsapp_phone="abc"))
        self.assertIn("whatsapp_phone", response.context["form"].errors)

        self.client.post(CREATE_URL, patient_data(whatsapp_phone="0333 5556667"))
        self.assertEqual(Patient.objects.get().whatsapp_number, "923335556667")

    def test_indian_clinic_gets_indian_example(self):
        clinic = make_clinic("Jaipur Clinic", country=Clinic.Country.INDIA, timezone="Asia/Kolkata")
        user = make_user(clinic, Membership.Role.RECEPTIONIST)
        self.login(user)
        response = self.client.post(CREATE_URL, patient_data(phone="12345"))
        self.assertContains(response, "e.g. 98765 43210")

        self.client.post(CREATE_URL, patient_data(phone="98765 43210"))
        self.assertEqual(Patient.objects.get(clinic=clinic).whatsapp_number, "919876543210")

    def test_age_instead_of_date_of_birth(self):
        self.login(self.doctor)
        self.client.post(CREATE_URL, patient_data(date_of_birth="", age_years="34"))
        patient = Patient.objects.get()
        self.assertEqual(patient.date_of_birth, years_before(timezone.localdate(), 34))
        self.assertTrue(patient.dob_is_estimated)
        self.assertEqual(patient.age, 34)

    def test_age_and_date_of_birth_together_is_an_error(self):
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(age_years="30"))
        self.assertIn("age_years", response.context["form"].errors)
        self.assertFalse(Patient.objects.exists())

    def test_age_out_of_range(self):
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(date_of_birth="", age_years="130"))
        self.assertIn("age_years", response.context["form"].errors)

    def test_future_date_of_birth_rejected(self):
        self.login(self.doctor)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        response = self.client.post(CREATE_URL, patient_data(date_of_birth=tomorrow))
        self.assertIn("date_of_birth", response.context["form"].errors)

    def test_no_date_of_birth_or_age_is_allowed(self):
        self.login(self.doctor)
        self.client.post(CREATE_URL, patient_data(date_of_birth=""))
        patient = Patient.objects.get()
        self.assertIsNone(patient.date_of_birth)
        self.assertFalse(patient.dob_is_estimated)

    def test_sex_must_be_a_choice(self):
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(sex="X"))
        self.assertIn("sex", response.context["form"].errors)


class DuplicateCheckTests(ClinicTestCase):
    def test_same_whatsapp_number_needs_confirmation(self):
        existing = self.make_patient(full_name="Ali Raza", phone="0300-1234567")
        self.login(self.receptionist)

        response = self.client.post(CREATE_URL, patient_data(phone="+92 300 1234567"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This patient may already be registered")
        self.assertContains(response, existing.get_absolute_url())
        self.assertContains(response, 'name="confirm_not_duplicate"')
        self.assertEqual(Patient.objects.count(), 1)

        response = self.client.post(CREATE_URL, patient_data(phone="+92 300 1234567", confirm_not_duplicate="on"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Patient.objects.count(), 2)

    def test_same_name_and_date_of_birth_needs_confirmation(self):
        self.make_patient(full_name="Ayesha Khan", phone="0321-7654321", date_of_birth=date(1988, 4, 15))
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data(full_name="ayesha  KHAN"))
        self.assertContains(response, "This patient may already be registered")
        self.assertEqual(Patient.objects.count(), 1)

    def test_confirm_box_hidden_when_no_duplicates(self):
        self.login(self.doctor)
        response = self.client.get(CREATE_URL)
        self.assertNotContains(response, 'name="confirm_not_duplicate"')

    def test_archived_and_other_clinic_patients_are_not_duplicates(self):
        self.make_patient(phone="0300-1234567", is_archived=True)
        make_patient(self.other_clinic, phone="0300-1234567")
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data())
        self.assertEqual(response.status_code, 302)

    def test_same_name_different_birthday_is_fine(self):
        self.make_patient(full_name="Ayesha Khan", phone="0321-7654321", date_of_birth=date(1990, 1, 1))
        self.login(self.doctor)
        response = self.client.post(CREATE_URL, patient_data())
        self.assertEqual(response.status_code, 302)


class PatientUpdateTests(ClinicTestCase):
    def update_url(self, patient):
        return reverse("patients:update", args=[patient.pk])

    def test_every_role_can_edit_demographics(self):
        patient = self.make_patient()
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(user=user):
                self.login(user)
                self.assertEqual(self.client.get(self.update_url(patient)).status_code, 200)

    def test_update_saves_and_audits(self):
        patient = self.make_patient(full_name="Ali Raza")
        self.login(self.doctor)
        response = self.client.post(self.update_url(patient), patient_data(full_name="Ali Raza Khan", phone="0300-1234567"))
        self.assertRedirects(response, patient.get_absolute_url(), fetch_redirect_response=False)
        patient.refresh_from_db()
        self.assertEqual(patient.full_name, "Ali Raza Khan")
        log = AuditLog.objects.get(action=AuditLog.Action.UPDATE)
        self.assertEqual(log.summary, f"Updated patient {patient.mrn}")

    def test_editing_does_not_trigger_duplicate_check_against_itself(self):
        patient = self.make_patient(full_name="Ayesha Khan", date_of_birth=date(1988, 4, 15))
        self.make_patient(full_name="Sister", phone="0300-1234567")
        self.login(self.doctor)
        response = self.client.post(self.update_url(patient), patient_data())
        self.assertEqual(response.status_code, 302)

    def test_receptionist_edit_keeps_clinical_fields(self):
        patient = self.make_patient(allergies="Penicillin", chronic_conditions="Diabetes", blood_group="A+")
        self.login(self.receptionist)
        response = self.client.get(self.update_url(patient))
        self.assertNotContains(response, "Penicillin")
        self.assertNotContains(response, "Diabetes")

        self.client.post(self.update_url(patient), patient_data(full_name="Ali Raza", allergies=""))
        patient.refresh_from_db()
        self.assertEqual(patient.allergies, "Penicillin")
        self.assertEqual(patient.chronic_conditions, "Diabetes")
        self.assertEqual(patient.blood_group, "A+")

    def test_doctor_can_change_clinical_fields(self):
        patient = self.make_patient(allergies="Penicillin")
        self.login(self.doctor)
        self.client.post(self.update_url(patient), patient_data(allergies="", chronic_conditions="Asthma"))
        patient.refresh_from_db()
        self.assertEqual(patient.allergies, "")
        self.assertEqual(patient.chronic_conditions, "Asthma")

    def test_other_clinic_patient_is_404(self):
        patient = make_patient(self.other_clinic)
        self.login(self.owner)
        self.assertEqual(self.client.get(self.update_url(patient)).status_code, 404)
        self.assertEqual(self.client.post(self.update_url(patient), patient_data()).status_code, 404)

    def test_estimated_birthday_stays_estimated_until_a_real_date_is_typed(self):
        dob = years_before(timezone.localdate(), 50)
        patient = self.make_patient(date_of_birth=dob, dob_is_estimated=True)
        self.login(self.doctor)

        self.client.post(self.update_url(patient), patient_data(date_of_birth=dob.isoformat(), city="Multan"))
        patient.refresh_from_db()
        self.assertTrue(patient.dob_is_estimated)

        # Typing a new age replaces the old estimate (the untouched date is not "both").
        self.client.post(self.update_url(patient), patient_data(date_of_birth=dob.isoformat(), age_years="52"))
        patient.refresh_from_db()
        self.assertEqual(patient.date_of_birth, years_before(timezone.localdate(), 52))
        self.assertTrue(patient.dob_is_estimated)

        self.client.post(self.update_url(patient), patient_data(date_of_birth="1975-01-20"))
        patient.refresh_from_db()
        self.assertEqual(patient.date_of_birth, date(1975, 1, 20))
        self.assertFalse(patient.dob_is_estimated)

    def test_form_requires_clinic_and_role(self):
        form = PatientForm(clinic=self.clinic, is_clinician=False)
        self.assertNotIn("allergies", form.fields)
        form = PatientForm(clinic=self.clinic, is_clinician=True)
        self.assertIn("allergies", form.fields)
