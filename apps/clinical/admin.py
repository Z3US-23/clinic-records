"""Platform-operator admin for clinical records (Django admin = superusers only, not clinic staff)."""

from django.contrib import admin

from .models import LabResult, PrescriptionItem, Visit


class PrescriptionItemInline(admin.TabularInline):
    model = PrescriptionItem
    extra = 0
    fields = ["order", "medicine", "dose", "frequency", "duration", "instructions"]


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ["visit_date", "patient", "doctor", "clinic", "diagnosis", "follow_up_date"]
    list_filter = ["clinic", "visit_date"]
    list_select_related = ["patient", "doctor", "clinic"]
    search_fields = ["patient__full_name", "patient__mrn", "chief_complaint", "diagnosis"]
    date_hierarchy = "visit_date"
    raw_id_fields = ["clinic", "patient", "doctor", "appointment"]
    readonly_fields = ["created_by", "created_at", "updated_at"]
    inlines = [PrescriptionItemInline]
    fieldsets = [
        (None, {"fields": ["clinic", "patient", "doctor", "appointment", "visit_date"]}),
        ("Notes", {"fields": ["chief_complaint", "history", "examination", "diagnosis", "plan", "follow_up_date"]}),
        (
            "Vitals",
            {
                "fields": [
                    ("bp_systolic", "bp_diastolic"),
                    ("pulse", "temperature_c", "spo2"),
                    ("weight_kg", "height_cm", "blood_sugar"),
                ]
            },
        ),
        ("Record", {"fields": ["created_by", "created_at", "updated_at"]}),
    ]


@admin.register(LabResult)
class LabResultAdmin(admin.ModelAdmin):
    list_display = ["test_name", "patient", "clinic", "result_date", "is_abnormal", "has_file"]
    list_filter = ["clinic", "is_abnormal", "result_date"]
    list_select_related = ["patient", "clinic"]
    search_fields = ["test_name", "patient__full_name", "patient__mrn"]
    raw_id_fields = ["clinic", "patient", "visit"]
    # Files are uploaded through the clinic app, where their contents are checked.
    readonly_fields = ["file", "original_filename", "uploaded_by", "created_at"]

    @admin.display(boolean=True, description="File")
    def has_file(self, lab):
        return bool(lab.file)
