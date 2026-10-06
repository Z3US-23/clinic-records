from django.urls import path

from . import views

app_name = "appointments"

urlpatterns = [
    path("", views.day_view, name="day"),
    path("week/", views.week_view, name="week"),
    path("new/", views.create_view, name="create"),
    path("<int:pk>/edit/", views.update_view, name="update"),
    path("<int:pk>/status/", views.set_status_view, name="set_status"),
]
