from django.urls import path

from apps.core.placeholders import placeholder

app_name = "appointments"

urlpatterns = [
    path("", placeholder, name="day"),
    path("week/", placeholder, name="week"),
    path("new/", placeholder, name="create"),
    path("<int:pk>/edit/", placeholder, name="update"),
    path("<int:pk>/status/", placeholder, name="set_status"),
]
