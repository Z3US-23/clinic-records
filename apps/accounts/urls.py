from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.SignInView.as_view(), name="login"),
    path("logout/", views.SignOutView.as_view(), name="logout"),
    path("password/", views.PasswordChangeView.as_view(), name="password_change"),
    path("password/done/", views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("signup/", views.SignupView.as_view(), name="signup"),
    path("no-clinic/", views.NoClinicView.as_view(), name="no_clinic"),
    path("switch-clinic/<int:clinic_id>/", views.switch_clinic, name="switch_clinic"),
    path("join/<str:token>/", views.JoinClinicView.as_view(), name="join"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("staff/", views.StaffListView.as_view(), name="staff_list"),
    path("staff/add/", views.StaffAddView.as_view(), name="staff_add"),
    path("staff/<int:pk>/edit/", views.StaffEditView.as_view(), name="staff_edit"),
    path("staff/<int:pk>/password/", views.StaffSetPasswordView.as_view(), name="staff_set_password"),
    path("staff/<int:pk>/invite/", views.staff_invite, name="staff_invite"),
    path("settings/", views.ClinicSettingsView.as_view(), name="clinic_settings"),
]
