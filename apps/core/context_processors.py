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
        "user_clinics": [],
    }
    if clinic is not None:
        from apps.accounts.models import Membership
        from apps.reminders.models import Reminder

        context["nav_reminders_due"] = Reminder.objects.filter(
            clinic=clinic, status=Reminder.Status.PENDING, due_date__lte=timezone.localdate()
        ).count()
        context["user_clinics"] = list(
            Membership.objects.filter(user=request.user, is_active=True, clinic__is_active=True)
            .select_related("clinic")
            .order_by("clinic__name")
        )
    return context
