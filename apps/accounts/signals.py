"""Write sign-in, sign-out and failed sign-in events to the audit log.

Connected in AccountsConfig.ready(). Passwords are never logged: Django hands
user_login_failed a copy of the credentials with the password blanked out,
and we only read the email from it anyway.
"""

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from apps.core.audit import Action, log_action

from .models import Membership, User

logger = logging.getLogger(__name__)


def active_memberships(user):
    """The memberships a user can work in: switched on, at a clinic that isn't paused.

    (A waiting invitation is never switched on, so it is never included.)
    """
    return Membership.objects.filter(
        user=user, is_active=True, accepted_at__isnull=False, clinic__is_active=True
    ).select_related("clinic")


def first_active_clinic(user):
    """The clinic a user lands in by default (same rule as CurrentClinicMiddleware)."""
    membership = active_memberships(user).order_by("created_at").first()
    return membership.clinic if membership else None


def targeted_account(email):
    """(user, [their active clinics]) for the account with this email, or (None, []).

    Never raises: a failed lookup must not break the sign-in page.
    """
    if not email:
        return None, []
    try:
        user = User.objects.filter(email=email).first()
        if user is None:
            return None, []
        return user, [membership.clinic for membership in active_memberships(user)]
    except Exception:  # pragma: no cover - defensive, like log_action()
        logger.exception("Could not look up the account for a failed sign-in")
        return None, []


@receiver(user_logged_in, dispatch_uid="accounts_audit_login")
def audit_login(sender, request, user, **kwargs):
    # login() starts a fresh session, so the user lands in their first clinic (see the
    # middleware). Moving to another clinic is logged by switch_clinic.
    log_action(request, Action.LOGIN, user, "Signed in", clinic=first_active_clinic(user), user=user)


@receiver(user_logged_out, dispatch_uid="accounts_audit_logout")
def audit_logout(sender, request, user, **kwargs):
    if user is None:  # signing out when nobody was signed in
        return
    clinic = getattr(request, "clinic", None) or first_active_clinic(user)
    log_action(request, Action.LOGOUT, user, "Signed out", clinic=clinic, user=user)


@receiver(user_login_failed, dispatch_uid="accounts_audit_login_failed")
def audit_login_failed(sender, credentials, request=None, **kwargs):
    """Show a wrong password in the audit log of every clinic the targeted account works at.

    An email with no account (or an account with no active clinic) gets one entry without
    a clinic, which only the platform operator sees in the Django admin. The sign-in page
    looks the same either way, so this reveals nothing about which emails exist.
    """
    email = str(credentials.get("username") or credentials.get("email") or "").strip().lower()[:150]
    summary = f"Failed sign-in for {email or 'unknown email'}"
    user, clinics = targeted_account(email)
    if not clinics:
        log_action(request, Action.LOGIN_FAILED, None, summary, user=user)
        return
    for clinic in clinics:
        log_action(request, Action.LOGIN_FAILED, None, summary, clinic=clinic, user=user)
