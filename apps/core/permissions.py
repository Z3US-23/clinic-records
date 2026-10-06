"""Access rules shared by every app.

Rules of the house:
  * Every page that shows clinic data uses one of these mixins/decorators.
  * Every queryset is filtered by `request.clinic` (use ClinicScopedMixin or
    `for_clinic(Model, request)`) — a clinic must never see another's patients.
  * Receptionists manage patients' contact details, appointments and reminders,
    but cannot see clinical notes, prescriptions or lab results.
"""

from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from apps.accounts.models import Membership

Role = Membership.Role
ANY_ROLE = None
CLINICAL_ROLES = Membership.CLINICAL_ROLES
OWNER_ONLY = (Role.OWNER,)


def _check(request, roles):
    """Return a response to short-circuit with, raise PermissionDenied, or return None if allowed."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if getattr(request, "clinic", None) is None:
        return redirect("accounts:no_clinic")
    if roles and request.membership.role not in roles:
        raise PermissionDenied("Your role does not have access to this page.")
    return None


class ClinicRequiredMixin(LoginRequiredMixin):
    """Signed in AND working in a clinic. Set `allowed_roles` to restrict further."""

    allowed_roles = ANY_ROLE

    def dispatch(self, request, *args, **kwargs):
        response = _check(request, self.allowed_roles)
        if response is not None:
            return response
        return super().dispatch(request, *args, **kwargs)


class ClinicianRequiredMixin(ClinicRequiredMixin):
    """Doctors and clinic owners only (clinical notes, prescriptions, lab results)."""

    allowed_roles = CLINICAL_ROLES


class OwnerRequiredMixin(ClinicRequiredMixin):
    """Clinic owner only (staff, settings, export, audit log)."""

    allowed_roles = OWNER_ONLY


class ClinicScopedMixin:
    """For Detail/Update/List views: limit the queryset to the current clinic."""

    def get_queryset(self):
        return super().get_queryset().filter(clinic=self.request.clinic)


def clinic_required(view_func=None, *, roles=ANY_ROLE):
    """Function-view version of the mixins: @clinic_required or @clinic_required(roles=CLINICAL_ROLES)."""

    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            response = _check(request, roles)
            if response is not None:
                return response
            return func(request, *args, **kwargs)

        return wrapper

    if view_func is not None:
        return decorator(view_func)
    return decorator


def for_clinic(model, request):
    """`model.objects` limited to the current clinic, e.g. for_clinic(Patient, request).get(pk=pk)."""
    return model.objects.filter(clinic=request.clinic)


def is_clinician(request):
    membership = getattr(request, "membership", None)
    return bool(membership and membership.is_clinician)


def is_owner(request):
    membership = getattr(request, "membership", None)
    return bool(membership and membership.is_owner)
