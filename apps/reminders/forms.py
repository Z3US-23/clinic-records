from django import forms
from django.utils import timezone

from .models import Reminder, ReminderKind

# WhatsApp allows long messages, but reminders should stay short; this also keeps
# the wa.me link a sensible length.
MESSAGE_MAX_LENGTH = 2000
TEMPLATE_MAX_LENGTH = 1000


def _message_field(**kwargs):
    return forms.CharField(
        label="Message",
        max_length=MESSAGE_MAX_LENGTH,
        widget=forms.Textarea(
            # reminders.js: character counter + live preview in #message-preview
            attrs={"rows": 8, "data-char-count": MESSAGE_MAX_LENGTH, "data-live-preview": "message-preview"}
        ),
        **kwargs,
    )


def _due_date_field(**kwargs):
    return forms.DateField(
        label="Send on",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        **kwargs,
    )


class ReminderMessageForm(forms.ModelForm):
    """Edit a pending reminder's text (and the day it should go out)."""

    message = _message_field()
    due_date = _due_date_field(help_text="The reminder shows under “To send” from this day.")

    class Meta:
        model = Reminder
        fields = ["message", "due_date"]

    def clean_due_date(self):
        due_date = self.cleaned_data["due_date"]
        # A reminder that is already late may keep its date; a new date can't be in the past.
        if "due_date" in self.changed_data and due_date < timezone.localdate():
            raise forms.ValidationError("Choose today or a later date.")
        return due_date


class CustomReminderForm(forms.Form):
    """A message written by staff to one patient."""

    message = _message_field()
    due_date = _due_date_field(
        required=False,
        help_text="Only used for “Save to send later”. Leave empty for today.",
    )

    def clean_due_date(self):
        due_date = self.cleaned_data.get("due_date")
        if due_date and due_date < timezone.localdate():
            raise forms.ValidationError("Choose today or a later date.")
        return due_date


class MessageTemplateForm(forms.Form):
    """The wording of one kind of reminder (owner only)."""

    kind = forms.ChoiceField(choices=ReminderKind.choices, widget=forms.HiddenInput)
    body = forms.CharField(
        label="Message",
        max_length=TEMPLATE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 10, "data-char-count": TEMPLATE_MAX_LENGTH}),
    )

    def __init__(self, *args, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        if kind:
            # reminders.js fills #preview-<kind> as the owner types, using that kind's sample values.
            self.fields["body"].widget.attrs.update({"data-live-preview": f"preview-{kind}", "data-kind": kind})


class PatientSearchForm(forms.Form):
    q = forms.CharField(
        label="Find patient",
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"type": "search", "placeholder": "Name, phone or MR no.", "autocomplete": "off"}
        ),
    )
