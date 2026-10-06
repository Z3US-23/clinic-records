from datetime import timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import dateformat, timezone

from apps.accounts.models import Clinic, Membership
from apps.appointments.models import Appointment
from apps.core.testing import ClinicTestCase, make_clinic, make_patient, make_user
from apps.patients.models import Patient
from apps.patients.services import search_patients

LIST_URL = reverse("patients:list")
SEARCH_URL = reverse("patients:search_json")


class PatientListTests(ClinicTestCase):
    def test_sign_in_required(self):
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_every_role_sees_the_list(self):
        self.make_patient(full_name="Ayesha Khan")
        for user in (self.owner, self.doctor, self.receptionist):
            with self.subTest(user=user):
                self.login(user)
                response = self.client.get(LIST_URL)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Ayesha Khan")
                self.assertContains(response, f'{reverse("appointments:create")}?patient=')

    def test_only_own_clinic_patients_are_listed(self):
        self.make_patient(full_name="Ayesha Khan")
        self.make_patient(clinic=self.other_clinic, full_name="Hidden Person")
        self.login(self.doctor)
        response = self.client.get(LIST_URL)
        self.assertContains(response, "Ayesha Khan")
        self.assertNotContains(response, "Hidden Person")

    def test_import_button_only_for_owner(self):
        self.login(self.owner)
        self.assertContains(self.client.get(LIST_URL), reverse("patients:import"))
        self.login(self.doctor)
        self.assertNotContains(self.client.get(LIST_URL), reverse("patients:import"))

    def test_archived_hidden_by_default_and_shown_with_flag(self):
        self.make_patient(full_name="Active Patient")
        self.make_patient(full_name="Old Patient", is_archived=True)
        self.login(self.doctor)

        response = self.client.get(LIST_URL)
        self.assertContains(response, "Active Patient")
        self.assertNotContains(response, "Old Patient")

        response = self.client.get(LIST_URL, {"archived": "1"})
        self.assertNotContains(response, "Active Patient")
        self.assertContains(response, "Old Patient")

    def test_search_by_name_mrn_and_phone(self):
        ayesha = self.make_patient(full_name="Ayesha Khan", phone="0300-1234567")
        bilal = self.make_patient(full_name="Bilal Ahmed", phone="0321-7654321")
        self.login(self.receptionist)

        cases = {
            "ayesha": ayesha,
            "KHAN": ayesha,
            bilal.mrn: bilal,
            bilal.mrn.lower(): bilal,
            "0300-1234567": ayesha,  # as typed
            "03001234567": ayesha,  # without the dash
            "0300 1234567": ayesha,
            "+92 300 1234567": ayesha,  # international
            "923217654321": bilal,
            "4567": ayesha,  # last digits
            "0321 765": bilal,  # partial with leading 0
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                response = self.client.get(LIST_URL, {"q": query})
                found = list(response.context["patients"])
                self.assertEqual(found, [expected])

    def test_search_matches_separate_whatsapp_number(self):
        patient = self.make_patient(phone="0300-1234567", whatsapp_phone="0333 5556667")
        self.login(self.doctor)
        response = self.client.get(LIST_URL, {"q": "03335556667"})
        self.assertEqual(list(response.context["patients"]), [patient])

    def test_search_indian_numbers(self):
        clinic = make_clinic("Delhi Clinic", country=Clinic.Country.INDIA, timezone="Asia/Kolkata")
        patient = make_patient(clinic, phone="98765 43210")
        make_patient(clinic, full_name="Someone Else", phone="91234 56789")
        found = search_patients(Patient.objects.filter(clinic=clinic), "+91 98765 43210", clinic.country)
        self.assertEqual(list(found), [patient])
        found = search_patients(Patient.objects.filter(clinic=clinic), "9876543210", clinic.country)
        self.assertEqual(list(found), [patient])

    def test_no_results_shows_empty_state(self):
        self.make_patient(full_name="Ayesha Khan")
        self.login(self.doctor)
        response = self.client.get(LIST_URL, {"q": "zzzz"})
        self.assertContains(response, "No patient found")
        self.assertContains(response, reverse("patients:create"))

    def test_empty_clinic_shows_add_patient(self):
        self.login(self.doctor)
        response = self.client.get(LIST_URL)
        self.assertContains(response, "No patients yet")
        self.assertContains(response, "Add patient")

    def test_last_visit_and_next_appointment_columns(self):
        patient = self.make_patient()
        visit_date = timezone.now() - timedelta(days=10)
        self.make_visit(patient, visit_date=visit_date)
        soon = timezone.now() + timedelta(days=3)
        self.make_appointment(patient, when=soon)
        # Cancelled and past appointments are not "next".
        self.make_appointment(patient, when=timezone.now() + timedelta(days=1), status=Appointment.Status.CANCELLED)
        self.make_appointment(patient, when=timezone.now() - timedelta(days=1))

        self.login(self.doctor)
        response = self.client.get(LIST_URL)
        row = response.context["patients"][0]
        self.assertEqual(row.last_visit, visit_date)
        self.assertEqual(row.next_appointment, soon)
        local_soon = timezone.localtime(soon, ZoneInfo(self.clinic.timezone))
        self.assertContains(response, dateformat.format(local_soon, "j M Y"))
        self.assertContains(response, dateformat.format(local_soon, "g:i a"))

    def test_sorting(self):
        old = self.make_patient(full_name="Zara Visitor")
        new = self.make_patient(full_name="Adam Newcomer")
        never = self.make_patient(full_name="Mona Never")
        self.make_visit(old, visit_date=timezone.now() - timedelta(days=1))
        self.make_visit(new, visit_date=timezone.now() - timedelta(days=30))
        self.login(self.doctor)

        by_name = list(self.client.get(LIST_URL, {"sort": "name"}).context["patients"])
        self.assertEqual(by_name, [new, never, old])
        by_recent = list(self.client.get(LIST_URL, {"sort": "recent"}).context["patients"])
        self.assertEqual(by_recent, [never, new, old])
        by_visit = list(self.client.get(LIST_URL, {"sort": "last_visit"}).context["patients"])
        self.assertEqual(by_visit, [old, new, never])
        # Unknown sort falls back to name.
        self.assertEqual(list(self.client.get(LIST_URL, {"sort": "bogus"}).context["patients"]), by_name)

    def test_paginates_25_per_page(self):
        for n in range(30):
            self.make_patient(full_name=f"Patient {n:02d}", phone=f"0300-12345{n:02d}")
        self.login(self.doctor)
        response = self.client.get(LIST_URL)
        self.assertEqual(len(response.context["patients"]), 25)
        self.assertContains(response, "Showing 1–25 of 30")
        response = self.client.get(LIST_URL, {"page": 2})
        self.assertEqual(len(response.context["patients"]), 5)
        # A page number that no longer exists (e.g. after archiving) shows the last page, not an error.
        for page in ("9", "abc"):
            with self.subTest(page=page):
                self.assertEqual(self.client.get(LIST_URL, {"page": page}).status_code, 200)

    def test_archived_rows_link_to_profile_instead_of_booking(self):
        patient = self.make_patient(full_name="Old Patient", is_archived=True)
        self.login(self.receptionist)
        response = self.client.get(LIST_URL, {"archived": "1"})
        self.assertContains(response, patient.get_absolute_url())
        self.assertNotContains(response, f'{reverse("appointments:create")}?patient={patient.pk}')

    def test_search_json_is_get_only(self):
        self.login(self.doctor)
        self.assertEqual(self.client.post(SEARCH_URL, {"q": "ali"}).status_code, 405)


class SearchJsonTests(ClinicTestCase):
    def test_response_shape(self):
        patient = self.make_patient(full_name="Ayesha Khan", sex=Patient.Sex.FEMALE, allergies="Penicillin")
        self.login(self.receptionist)
        response = self.client.get(SEARCH_URL, {"q": "ayesha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "results": [
                    {
                        "id": patient.pk,
                        "name": "Ayesha Khan",
                        "mrn": patient.mrn,
                        "phone": "0300-1234567",
                        "age_sex": f"{patient.age} y · F",
                        "url": reverse("patients:detail", args=[patient.pk]),
                    }
                ]
            },
        )
        self.assertNotIn("Penicillin", response.content.decode())

    def test_age_unknown_shows_sex_only(self):
        self.make_patient(full_name="Ayesha Khan", sex=Patient.Sex.FEMALE, date_of_birth=None)
        self.login(self.doctor)
        result = self.client.get(SEARCH_URL, {"q": "Ayesha"}).json()["results"][0]
        self.assertEqual(result["age_sex"], "F")

    def test_short_query_returns_nothing(self):
        self.make_patient(full_name="Ali Raza")
        self.login(self.doctor)
        for query in ("", "a", " a "):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(SEARCH_URL, {"q": query}).json(), {"results": []})

    def test_at_most_eight_ordered_by_name_and_no_archived(self):
        for n in range(10):
            self.make_patient(full_name=f"Khan {9 - n}", phone=f"0300-12345{n:02d}")
        self.make_patient(full_name="Khan Archived", is_archived=True, phone="0300-9999999")
        self.login(self.doctor)
        results = self.client.get(SEARCH_URL, {"q": "khan"}).json()["results"]
        self.assertEqual(len(results), 8)
        self.assertEqual([r["name"] for r in results], [f"Khan {n}" for n in range(8)])

    def test_other_clinic_not_searchable(self):
        make_patient(self.other_clinic, full_name="Secret Person")
        self.login(self.doctor)
        self.assertEqual(self.client.get(SEARCH_URL, {"q": "Secret"}).json(), {"results": []})

    def test_phone_search(self):
        patient = self.make_patient(phone="0300-1234567")
        self.login(self.doctor)
        results = self.client.get(SEARCH_URL, {"q": "03001234567"}).json()["results"]
        self.assertEqual([r["id"] for r in results], [patient.pk])

    def test_sign_in_required(self):
        self.assertEqual(self.client.get(SEARCH_URL, {"q": "ali"}).status_code, 302)

    def test_user_without_clinic_is_sent_to_no_clinic_page(self):
        user = make_user(self.clinic, Membership.Role.DOCTOR)
        Membership.objects.filter(user=user).update(is_active=False)
        self.login(user)
        response = self.client.get(SEARCH_URL, {"q": "ali"})
        self.assertRedirects(response, reverse("accounts:no_clinic"), fetch_redirect_response=False)
