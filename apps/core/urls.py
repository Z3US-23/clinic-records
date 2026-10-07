from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("follow-ups/missed/", views.missed_follow_ups_view, name="missed_follow_ups"),
    path("setup-checklist/hide/", views.hide_setup_checklist, name="hide_setup_checklist"),
    path("audit-log/", views.audit_log, name="audit_log"),
    path("export/", views.export_data, name="export"),
    # Installable app (PWA). The service worker is served from the site root so it controls every page.
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", views.offline, name="offline"),
]
