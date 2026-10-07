from django.contrib import admin

from .models import MessageTemplate, Reminder


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("patient", "kind", "status", "due_date", "clinic", "sent_at", "sent_by")
    list_filter = ("status", "skip_reason", "kind", "channel", "clinic")
    search_fields = ("patient__full_name", "patient__mrn", "clinic__name")
    date_hierarchy = "due_date"
    list_select_related = ("patient", "clinic", "sent_by")
    raw_id_fields = ("patient", "appointment", "visit", "sent_by", "created_by")
    readonly_fields = ("created_at",)
    ordering = ("-due_date", "-pk")


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("clinic", "kind", "language", "updated_at")
    list_filter = ("kind", "language")
    search_fields = ("clinic__name", "body")
    list_select_related = ("clinic",)
    readonly_fields = ("updated_at",)
