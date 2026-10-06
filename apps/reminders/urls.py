from django.urls import path

from apps.core.placeholders import placeholder

app_name = "reminders"

urlpatterns = [
    path("", placeholder, name="list"),
    path("new/", placeholder, name="create"),
    path("templates/", placeholder, name="templates"),
    path("<int:pk>/send/", placeholder, name="send"),
    path("<int:pk>/skip/", placeholder, name="skip"),
    path("<int:pk>/edit/", placeholder, name="update"),
]
