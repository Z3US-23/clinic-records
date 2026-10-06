from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only view of the audit trail for the platform operator.

    An audit log is only trustworthy if nobody can change it, so the admin can look and search
    but never add, edit or delete entries.
    """

    list_display = ("created_at", "clinic", "user", "action", "object_type", "object_id", "summary", "ip_address")
    list_filter = ("action", "clinic")
    list_select_related = ("clinic", "user")
    search_fields = ("summary", "object_type", "object_id", "user__email", "user__full_name", "ip_address")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
