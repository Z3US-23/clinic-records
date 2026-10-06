from datetime import datetime, timedelta

from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.formats import time_format

from apps.accounts.models import User

from . import status as appt_status
from .models import Appointment
from .scheduling import MAX_DURATION_MINUTES, PAST_GRACE, doctor_names, find_clash

Status = Appointment.Status

# How far ahead an appointment can be booked (catches typos like 2062 for 2026).
MAX_DAYS_AHEAD = 2 * 365


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None):
        super().__init__(attrs, format="%Y-%m-%d")


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, attrs=None):
        super().__init__(attrs, format="%H:%M")


class AppointmentForm(forms.ModelForm):
    """Book an appointment for a patient who has already been chosen.

    Date and time are separate inputs; they are combined in the clinic's timezone
    (activated by the middleware) into `scheduled_at`.
    """

    walk_in = forms.BooleanField(
        label="Patient is here now",
        required=False,
        help_text="Walk-in: adds the patient to today's waiting room straight away. Date and time are not needed.",
    )
    date = forms.DateField(required=False, widget=DateInput())
    time = forms.TimeField(required=False, widget=TimeInput(attrs={"step": 300}))
    duration_minutes = forms.IntegerField(
        label="Duration (minutes)", min_value=5, max_value=MAX_DURATION_MINUTES
    )
    allow_double_booking = forms.BooleanField(
        label="Book anyway (double booking)",
        required=False,
        help_text="The doctor will have two patients booked at the same time.",
    )

    class Meta:
        model = Appointment
        fields = ["doctor", "duration_minutes", "reason"]
        labels = {"reason": "Reason for visit"}
        widgets = {"reason": forms.TextInput(attrs={"placeholder": "e.g. Follow-up, fever, BP check"})}

    field_order = ["walk_in", "date", "time", "doctor", "duration_minutes", "reason", "status", "allow_double_booking"]

    def __init__(self, *args, clinic, **kwargs):
        super().__init__(*args, **kwargs)
        self.clinic = clinic
        self.scheduled_at = None  # worked out in clean()
        self.clash = None  # the other appointment, when the doctor is already busy at that time

        names = doctor_names(clinic)
        doctor = self.fields["doctor"]
        # Only this clinic's doctors: a crafted id from another clinic fails validation.
        doctor.queryset = clinic.doctors
        doctor.empty_label = "Any doctor"
        doctor.help_text = ""
        doctor.label_from_instance = lambda user: names.get(user.pk, user.full_name)

    # --- what changed -------------------------------------------------------

    @property
    def is_new(self):
        return self.instance.pk is None

    @property
    def schedule_changed(self):
        """True when booking, or when the date/time inputs were changed on edit."""
        return self.is_new or bool({"date", "time"} & set(self.changed_data))

    @property
    def rescheduled(self):
        """An existing appointment moved to another time or doctor (the reminder must be re-prepared)."""
        return not self.is_new and bool({"date", "time", "doctor"} & set(self.changed_data))

    # --- validation ---------------------------------------------------------

    def clean_date(self):
        day = self.cleaned_data.get("date")
        if day and day > timezone.localdate() + timedelta(days=MAX_DAYS_AHEAD):
            raise forms.ValidationError("That date is too far ahead. Please check the year.")
        return day

    def clean(self):
        cleaned = super().clean()

        if cleaned.get("walk_in"):
            self.scheduled_at = timezone.now()
            return cleaned

        day, at = cleaned.get("date"), cleaned.get("time")
        if day is None and "date" not in self.errors:
            self.add_error("date", "Choose a date.")
        if at is None and "time" not in self.errors:
            self.add_error("time", "Choose a time.")
        if day is None or at is None:
            return cleaned

        if self.schedule_changed:
            self.scheduled_at = timezone.make_aware(datetime.combine(day, at.replace(second=0, microsecond=0)))
            if self._must_be_in_future(cleaned) and self.scheduled_at < timezone.now() - PAST_GRACE:
                self.add_error(
                    "time",
                    "This time has already passed. If the patient is here now, tick “Patient is here now”."
                    if self.is_new else "This time has already passed.",
                )
                return cleaned
        else:
            # Editing other details only: keep the exact original time (walk-ins have seconds).
            self.scheduled_at = self.instance.scheduled_at

        self._check_double_booking(cleaned)
        return cleaned

    def _must_be_in_future(self, cleaned):
        """New bookings and upcoming appointments need a time that has not passed yet.

        Appointments that already happened (waiting, seen, did not come) or were cancelled
        may have their time corrected to an earlier one.
        """
        if self.is_new:
            return True
        new_status = cleaned.get("status", self.instance.status)
        return new_status in appt_status.UPCOMING_STATUSES

    def _needs_double_booking_check(self, cleaned):
        if self.is_new:
            return True
        new_status = cleaned.get("status", self.instance.status)
        if new_status not in Appointment.ACTIVE_STATUSES:
            return False
        moved = bool({"date", "time", "doctor", "duration_minutes"} & set(self.changed_data))
        reopened = self.initial.get("status") not in Appointment.ACTIVE_STATUSES
        return moved or reopened

    def _check_double_booking(self, cleaned):
        doctor = cleaned.get("doctor")
        minutes = cleaned.get("duration_minutes")
        if doctor is None or minutes is None or not self._needs_double_booking_check(cleaned):
            return
        clash = find_clash(self.clinic, doctor, self.scheduled_at, minutes, exclude_pk=self.instance.pk)
        if clash is None or cleaned.get("allow_double_booking"):
            return
        self.clash = clash
        start = timezone.localtime(clash.scheduled_at)
        end = timezone.localtime(clash.ends_at)
        doctor_name = self.fields["doctor"].label_from_instance(doctor)
        self.add_error(
            None,
            f"{doctor_name} already has {clash.patient.full_name} from {time_format(start, 'g:i a')} "
            f"to {time_format(end, 'g:i a')}. Choose another time, or tick “Book anyway (double booking)”.",
        )

    # --- saving -------------------------------------------------------------

    def save(self, commit=True):
        appointment = super().save(commit=False)
        appointment.scheduled_at = self.scheduled_at
        if self.cleaned_data.get("walk_in"):
            appointment.status = Status.ARRIVED
        if commit:
            appointment.save()
        return appointment


class AppointmentUpdateForm(AppointmentForm):
    """Edit an appointment: the booking fields plus its status (only allowed changes are offered)."""

    class Meta(AppointmentForm.Meta):
        fields = ["doctor", "duration_minutes", "reason", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["walk_in"]
        appointment = self.instance

        local = timezone.localtime(appointment.scheduled_at)
        self.initial.setdefault("date", local.date())
        self.initial.setdefault("time", local.time().replace(second=0, microsecond=0))
        if local.minute % 5:
            # e.g. a walk-in booked at 10:03: let the browser accept a time off the 5-minute grid.
            self.fields["time"].widget.attrs["step"] = 60

        status = self.fields["status"]
        status.choices = appt_status.allowed_choices(appointment.status)
        status.help_text = "Moving the date, time or doctor sets a confirmed appointment back to “Booked”."

        # Keep the current doctor selectable even if they have since left the clinic.
        if appointment.doctor_id:
            doctor = self.fields["doctor"]
            doctor.queryset = User.objects.filter(
                Q(pk__in=self.clinic.doctors.values("pk")) | Q(pk=appointment.doctor_id)
            )

    def save(self, commit=True):
        appointment = super().save(commit=False)
        if self.rescheduled:
            # The patient's earlier answer was about the old time.
            appointment.patient_responded_at = None
            if "status" not in self.changed_data and appointment.status in (
                Status.CONFIRMED, Status.RESCHEDULE_REQUESTED,
            ):
                appointment.status = Status.SCHEDULED
        if commit:
            appointment.save()
        return appointment


class PatientResponseForm(forms.Form):
    """The patient's answer on the public confirmation page."""

    CONFIRM = "confirm"
    RESCHEDULE = "reschedule"

    answer = forms.ChoiceField(choices=[(CONFIRM, "Yes, I will come"), (RESCHEDULE, "I need another time")])
    note = forms.CharField(
        label="Message for the clinic (optional)",
        required=False,
        max_length=255,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "e.g. Friday evening is better for me"}),
    )
