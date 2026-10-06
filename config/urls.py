from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Clinic Records – platform admin"
admin.site.site_title = "Clinic Records admin"

urlpatterns = [
    path("", include("apps.core.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("patients/", include("apps.patients.urls")),
    path("clinical/", include("apps.clinical.urls")),
    path("appointments/", include("apps.appointments.urls")),
    path("reminders/", include("apps.reminders.urls")),
    # Short public link sent to patients on WhatsApp: /c/<token>/
    path("c/", include("apps.appointments.public_urls")),
    path("admin/", admin.site.urls),
]
