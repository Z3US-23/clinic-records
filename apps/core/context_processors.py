from django.conf import settings
from django.utils import timezone


def app_context(request):
    clinic = getattr(request, "clinic", None)
    membership = getattr(request, "membership", None)
    context = {
        "PRODUCT_NAME": settings.PRODUCT_NAME,
        "current_clinic": clinic,
        "current_membership": membership,
        "is_clinician": bool(membership and membership.is_clinician),
        "is_owner": bool(membership and membership.is_owner),
        "nav_reminders_due": 0,
        "nav_reschedule_requests": 0,
        "user_clinics": [],
    }
    if clinic is not None:
        from apps.accounts.models import Membership
        from apps.reminders.models import Reminder

        from .services import reschedule_requests

        today = timezone.localdate()
        context["nav_reminders_due"] = Reminder.objects.filter(
            clinic=clinic, status=Reminder.Status.PENDING, due_date__lte=today
        ).count()
        # Patients waiting for a call back about another time: shown on "Today" from every page.
        context["nav_reschedule_requests"] = reschedule_requests(clinic, today).count()
        context["user_clinics"] = list(
            Membership.objects.filter(
                user=request.user, is_active=True, accepted_at__isnull=False, clinic__is_active=True
            )
            .select_related("clinic")
            .order_by("clinic__name")
        )
    return context
