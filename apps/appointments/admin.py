from django.contrib import admin

from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    """Platform-operator view. Clinic staff use the app's own pages, not the admin."""

    list_display = ("patient", "clinic", "scheduled_at", "status", "doctor")
    list_filter = ("status", "clinic", "scheduled_at")
    list_select_related = ("patient", "clinic", "doctor")
    search_fields = ("patient__full_name", "patient__mrn", "patient__phone")
    date_hierarchy = "scheduled_at"
    ordering = ("-scheduled_at",)
    raw_id_fields = ("patient", "doctor", "created_by")
    readonly_fields = ("confirm_token", "patient_responded_at", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("clinic", "patient", "doctor", "scheduled_at", "duration_minutes", "reason", "status")}),
        ("Patient's answer", {"fields": ("confirm_token", "patient_responded_at", "patient_note")}),
        ("Record", {"fields": ("created_by", "created_at", "updated_at")}),
    )
