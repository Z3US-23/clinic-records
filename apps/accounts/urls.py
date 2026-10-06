from django.urls import path

from apps.core.placeholders import placeholder

app_name = "accounts"

urlpatterns = [
    path("login/", placeholder, name="login"),
    path("logout/", placeholder, name="logout"),
    path("password/", placeholder, name="password_change"),
    path("password/done/", placeholder, name="password_change_done"),
    path("signup/", placeholder, name="signup"),
    path("no-clinic/", placeholder, name="no_clinic"),
    path("switch-clinic/<int:clinic_id>/", placeholder, name="switch_clinic"),
    path("profile/", placeholder, name="profile"),
    path("staff/", placeholder, name="staff_list"),
    path("staff/add/", placeholder, name="staff_add"),
    path("staff/<int:pk>/edit/", placeholder, name="staff_edit"),
    path("staff/<int:pk>/password/", placeholder, name="staff_set_password"),
    path("settings/", placeholder, name="clinic_settings"),
]
