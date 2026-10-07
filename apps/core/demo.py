"""The public online demo (settings.DEMO_MODE).

Anyone can try the app with the made-up "Demo Family Clinic" that `manage.py seed_demo` creates:

* One-click sign-in as the demo doctor, receptionist or clinic owner, so no password is shared.
* A banner on every page: made-up data only, and everything is wiped when the demo restarts.
* A few actions are switched off (DemoGuardMiddleware). Some would spoil the demo for the next
  visitor, e.g. changing the demo staff's passwords or roles. Uploads are off so strangers can't
  store files on the server.

NEVER turn DEMO_MODE on for a site with real patients: one-click sign-in lets anyone in.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.http import Http404
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership, User
from apps.core.management.commands.seed_demo import DEMO_PHONE_PREFIX, DEMO_SLUG, STAFF

from .middleware import CurrentClinicMiddleware

_DEMO_EMAILS = {person["key"]: person["email"] for person in STAFF}

# Role in the URL -> (seed_demo staff key, button label, what that person can do).
DEMO_ROLES = {
    "doctor": ("doctor", "Doctor", "Patient histories, visits, prescriptions and reminders."),
    "receptionist": ("reception", "Receptionist", "Appointments, waiting room and WhatsApp reminders. No medical notes."),
    "owner": ("owner", "Clinic owner", "Everything, plus staff, clinic settings, export and the audit log."),
}


# Demo numbers in international form: 0390-1234567 -> 923901234567. No network uses 0390.
DEMO_INTERNATIONAL_PREFIX = "92" + DEMO_PHONE_PREFIX.lstrip("0")


def is_made_up_number(international_number):
    """True for the demo's made-up 0390- mobiles. In the demo every patient number must be one."""
    return international_number.startswith(DEMO_INTERNATIONAL_PREFIX)


def demo_roles():
    """For the sign-in page: [{"role", "label", "description"}, ...]."""
    return [
        {"role": role, "label": label, "description": description}
        for role, (_key, label, description) in DEMO_ROLES.items()
    ]


@require_POST
def demo_login(request, role):
    """Sign in as one of the demo clinic's staff. Only exists when DEMO_MODE is on."""
    if not settings.DEMO_MODE or role not in DEMO_ROLES:
        raise Http404
    key, label, _description = DEMO_ROLES[role]
    user = User.objects.filter(email=_DEMO_EMAILS[key], is_active=True).first()
    membership = None
    # Only ever into the made-up demo clinic, and never as someone who also works at a real one.
    if user is not None and not user.memberships.exclude(clinic__slug=DEMO_SLUG).exists():
        membership = (
            Membership.objects.filter(
                user=user, clinic__slug=DEMO_SLUG, is_active=True, accepted_at__isnull=False, clinic__is_active=True
            )
            .select_related("clinic", "user")
            .first()
        )
    if membership is None:
        messages.error(request, "The demo clinic is still being set up. Please try again in a minute.")
        return redirect("accounts:login")

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[CurrentClinicMiddleware.SESSION_KEY] = membership.clinic_id
    messages.success(request, f"You're trying the demo as {membership.display_name}, {label.lower()}.")
    return redirect("core:dashboard")


class DemoGuardMiddleware:
    """In the online demo, refuse the few changes that would spoil it for the next visitor.

    Everything else (adding patients, recording visits, booking, sending reminders, editing
    message templates...) works, so people can really try the app. Their changes are wiped
    whenever the demo restarts.
    """

    # URL name -> what is switched off, for the message.
    BLOCKED_VIEWS = {
        "accounts:password_change": "Changing passwords",
        "accounts:profile": "Editing the demo staff's details",
        "accounts:staff_add": "Adding staff",
        "accounts:staff_edit": "Changing staff roles and access",
        "accounts:staff_set_password": "Setting staff passwords",
        "accounts:staff_invite": "Staff invitations",
        "accounts:clinic_settings": "Changing the clinic's settings",
        "accounts:signup": "Registering a new clinic",
        "accounts:join": "Joining a clinic",
        "patients:import": "Importing patients from a file",
    }
    # Any form bigger than this is refused before the server reads it (real forms are a few KB),
    # so visitors can't fill the demo's disk with huge notes or files.
    MAX_BODY_BYTES = 256 * 1024

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.DEMO_MODE and request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > self.MAX_BODY_BYTES:
                what = "Uploading files" if request.content_type == "multipart/form-data" else "Saving this much text"
                return self._refuse(request, what)
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not settings.DEMO_MODE or request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        match = request.resolver_match
        what = self.BLOCKED_VIEWS.get(match.view_name if match else "")
        if what is None and request.FILES:
            what = "Uploading files"
        if what is None:
            return None
        return self._refuse(request, what)

    @staticmethod
    def _refuse(request, what):
        messages.warning(
            request, f"{what} is switched off in the online demo, so the next visitor finds it as it should be."
        )
        back = request.META.get("HTTP_REFERER", "")
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            back = "/"
        return redirect(back)
