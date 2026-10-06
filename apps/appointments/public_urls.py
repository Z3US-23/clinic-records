"""Public pages patients open from WhatsApp. No login; access is by secret token only."""

from django.urls import path

from . import public_views

app_name = "public"

urlpatterns = [
    path("<str:token>/", public_views.confirm_view, name="confirm"),
]
