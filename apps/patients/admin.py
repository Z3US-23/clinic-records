from django.contrib import admin

from .models import Patient


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("full_name", "mrn", "clinic", "phone", "is_archived")
    list_filter = ("clinic", "is_archived")
    search_fields = ("full_name", "mrn", "phone", "whatsapp_number")
    list_select_related = ("clinic",)
    readonly_fields = ("whatsapp_number", "created_by", "created_at", "updated_at")
    ordering = ("clinic", "full_name")
