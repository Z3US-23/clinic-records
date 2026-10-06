from django.urls import path

from . import views

app_name = "reminders"

urlpatterns = [
    path("", views.ReminderListView.as_view(), name="list"),
    path("new/", views.CustomReminderCreateView.as_view(), name="create"),
    path("templates/", views.MessageTemplatesView.as_view(), name="templates"),
    path("<int:pk>/send/", views.SendReminderView.as_view(), name="send"),
    path("<int:pk>/skip/", views.SkipReminderView.as_view(), name="skip"),
    path("<int:pk>/edit/", views.ReminderUpdateView.as_view(), name="update"),
]
