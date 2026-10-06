from django.urls import path

from .placeholders import placeholder

app_name = "core"

urlpatterns = [
    path("", placeholder, name="dashboard"),
    path("audit-log/", placeholder, name="audit_log"),
    path("export/", placeholder, name="export"),
    path("manifest.webmanifest", placeholder, name="manifest"),
    path("sw.js", placeholder, name="service_worker"),
    path("offline/", placeholder, name="offline"),
]
