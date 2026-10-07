"""Make people choose their own password after a clinic owner has set one for them."""

from django.contrib import messages
from django.shortcuts import redirect


class PasswordChangeRequiredMiddleware:
    """Send a signed-in user with `must_change_password` to the "Change password" page.

    A password a clinic owner chose (new staff account, or a reset) is only good for the
    first sign-in: the owner knows it. Until the person picks their own, every page except
    the ones below redirects to accounts:password_change.

    It sits in settings.MIDDLEWARE after AuthenticationMiddleware and MessageMiddleware
    (right after apps.core.middleware.CurrentClinicMiddleware). Static files never reach
    it: WhiteNoise serves them earlier.
    """

    ALLOWED_VIEWS = {
        "accounts:password_change",
        "accounts:password_change_done",
        "accounts:logout",
        # The installable-app files are public and hold no clinic data.
        "core:manifest",
        "core:service_worker",
        "core:offline",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = request.user
        if not (user.is_authenticated and user.must_change_password):
            return None
        if request.resolver_match.view_name in self.ALLOWED_VIEWS:
            return None
        messages.info(request, "Please choose your own password before you continue.")
        return redirect("accounts:password_change")
