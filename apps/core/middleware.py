from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.cache import add_never_cache_headers

from apps.accounts.models import Membership


class CurrentClinicMiddleware:
    """Works out which clinic the signed-in user is working in.

    Sets `request.clinic` and `request.membership` (both None when signed out or
    when the user belongs to no active clinic) and switches dates/times to the
    clinic's timezone. Every view must filter data by `request.clinic`.
    """

    SESSION_KEY = "clinic_id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.clinic = None
        request.membership = None

        if request.user.is_authenticated:
            # A waiting invitation (accepted_at empty) is always inactive; checking both is a
            # second lock in case a bulk .update() ever switched one on.
            memberships = Membership.objects.filter(
                user=request.user, is_active=True, accepted_at__isnull=False, clinic__is_active=True
            ).select_related("clinic", "user")
            membership = None
            chosen = request.session.get(self.SESSION_KEY)
            if chosen:
                membership = memberships.filter(clinic_id=chosen).first()
            if membership is None:
                membership = memberships.order_by("created_at").first()
            if membership is not None:
                request.membership = membership
                request.clinic = membership.clinic
                if chosen != membership.clinic_id:
                    request.session[self.SESSION_KEY] = membership.clinic_id
                timezone.activate(ZoneInfo(membership.clinic.timezone))

        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()


class PrivateCacheControlMiddleware:
    """Pages shown to signed-in staff contain patient data: never let browsers or proxies cache them."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(request, "user", None) is not None and request.user.is_authenticated:
            if not response.has_header("Cache-Control"):
                add_never_cache_headers(response)
        return response
