"""Audit trail helper. Call log_action() whenever someone views or changes patient data."""

import logging

from django.conf import settings

from .models import AuditLog

logger = logging.getLogger(__name__)

Action = AuditLog.Action


def get_client_ip(request):
    if request is None:
        return None
    if getattr(settings, "USE_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def log_action(request, action, obj=None, summary="", *, clinic=None, user=None):
    """Record an audit entry. Never raises: auditing must not break the page.

    `obj` may be any model instance; its class name and pk are stored.
    `clinic` / `user` default to the ones on the request.
    """
    try:
        if clinic is None and request is not None:
            clinic = getattr(request, "clinic", None)
        if user is None and request is not None and request.user.is_authenticated:
            user = request.user
        AuditLog.objects.create(
            clinic=clinic,
            user=user,
            action=action,
            object_type=obj.__class__.__name__ if obj is not None else "",
            object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
            summary=(summary or "")[:255],
            ip_address=get_client_ip(request),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Could not write audit log entry")
