"""Write sign-in, sign-out and failed sign-in events to the audit log.

Connected in AccountsConfig.ready(). Passwords are never logged: Django hands
user_login_failed a copy of the credentials with the password blanked out,
and we only read the email from it anyway.
"""

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from apps.core.audit import Action, log_action

from .models import Membership


def first_active_clinic(user):
    """The clinic a user lands in by default (same rule as CurrentClinicMiddleware)."""
    membership = (
        Membership.objects.filter(user=user, is_active=True, clinic__is_active=True)
        .select_related("clinic")
        .order_by("created_at")
        .first()
    )
    return membership.clinic if membership else None


@receiver(user_logged_in, dispatch_uid="accounts_audit_login")
def audit_login(sender, request, user, **kwargs):
    log_action(request, Action.LOGIN, user, "Signed in", clinic=first_active_clinic(user), user=user)


@receiver(user_logged_out, dispatch_uid="accounts_audit_logout")
def audit_logout(sender, request, user, **kwargs):
    if user is None:  # signing out when nobody was signed in
        return
    clinic = getattr(request, "clinic", None) or first_active_clinic(user)
    log_action(request, Action.LOGOUT, user, "Signed out", clinic=clinic, user=user)


@receiver(user_login_failed, dispatch_uid="accounts_audit_login_failed")
def audit_login_failed(sender, credentials, request=None, **kwargs):
    email = str(credentials.get("username") or credentials.get("email") or "").strip()[:150]
    log_action(request, Action.LOGIN_FAILED, None, f"Failed sign-in for {email or 'unknown email'}")
