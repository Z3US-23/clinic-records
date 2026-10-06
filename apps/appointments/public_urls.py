"""Public pages patients open from WhatsApp. No login; access is by secret token only."""

from django.urls import path

from apps.core.placeholders import placeholder

app_name = "public"

urlpatterns = [
    path("<str:token>/", placeholder, name="confirm"),
]
