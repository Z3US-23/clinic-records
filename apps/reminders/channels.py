"""How a prepared reminder reaches the patient.

Version 1 uses one-tap WhatsApp links: the app prepares the message, staff tap
"Send on WhatsApp", and their own WhatsApp opens with the chat and text ready.
Nothing is sent by the server and there are no network calls here.

The channel classes keep that decision in one place, so automatic sending can be
added later without changing the views (see WhatsAppCloudAPIChannel).
"""

from apps.core.phone import whatsapp_link

from .models import Reminder


class WhatsAppLinkChannel:
    """One-tap sending through a https://wa.me/<number>?text=... link."""

    code = Reminder.Channel.WHATSAPP_LINK

    def url_for(self, reminder):
        """The wa.me link that opens this patient's chat with the message typed in,
        or None when the patient has no usable WhatsApp number."""
        number = reminder.patient.whatsapp_number
        if not number:
            return None
        return whatsapp_link(number, reminder.message)


class WhatsAppCloudAPIChannel:
    """Automatic sending through Meta's WhatsApp Business Cloud API. NOT BUILT YET.

    How it would work when a clinic wants reminders sent without a tap:

    1. The clinic registers a WhatsApp Business number with Meta and gets a
       phone-number ID and a long-lived access token (kept in environment
       variables, never in the database or the code).
    2. Business-initiated messages must use templates that Meta has approved in
       advance. Our MessageTemplate rows would be submitted for approval, and the
       placeholders sent as template parameters instead of being filled in here.
    3. A daily job (the `generate_reminders` management command) would call
       `send()` for each due reminder: POST to
       https://graph.facebook.com/<version>/<phone-number-id>/messages, store the
       returned message id, then mark the reminder SENT with channel WHATSAPP_API.
    4. A webhook view would receive delivery receipts and patient replies
       ("1 = confirm", "2 = another time") and update the appointment.

    Until then this class only documents the plan.
    """

    code = Reminder.Channel.WHATSAPP_API

    def url_for(self, reminder):
        return None

    def send(self, reminder):
        raise NotImplementedError(
            "Automatic WhatsApp sending is not available yet. Use the one-tap WhatsApp link instead."
        )


one_tap = WhatsAppLinkChannel()


def get_channel(reminder=None):
    """The channel used to send reminders. Always one-tap WhatsApp in this version."""
    return one_tap
