"""Django admin for the platform operator (not for clinic staff).

Users sign in with their email, so the stock UserAdmin is adapted: there is no
username, first name or last name — just email and full name.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm as BaseAdminUserCreationForm
from django.contrib.auth.forms import UserChangeForm as BaseUserChangeForm

from .models import Clinic, Membership, User


class AdminUserCreationForm(BaseAdminUserCreationForm):
    class Meta:
        model = User
        fields = ("email", "full_name")


class AdminUserChangeForm(BaseUserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


class UserMembershipInline(admin.TabularInline):
    """On a user's page: the clinics they work at."""

    model = Membership
    fk_name = "user"
    extra = 0
    autocomplete_fields = ["clinic"]
    fields = ["clinic", "role", "title", "qualifications", "registration_number", "is_active"]


class ClinicMembershipInline(admin.TabularInline):
    """On a clinic's page: its staff."""

    model = Membership
    fk_name = "clinic"
    extra = 0
    autocomplete_fields = ["user"]
    fields = ["user", "role", "title", "qualifications", "registration_number", "is_active"]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = AdminUserChangeForm
    add_form = AdminUserCreationForm
    inlines = [UserMembershipInline]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name",)}),
        (
            "Platform permissions",
            {
                "description": "These control the Django admin only. What someone can do inside "
                "a clinic comes from their clinic membership role.",
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "full_name", "usable_password", "password1", "password2"),
            },
        ),
    )
    list_display = ("email", "full_name", "is_active", "is_staff", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "full_name")
    ordering = ("email",)
    readonly_fields = ("last_login", "date_joined")


@admin.register(Clinic)
class ClinicAdmin(admin.ModelAdmin):
    inlines = [ClinicMembershipInline]
    list_display = ("name", "city", "country", "phone", "is_active", "created_at")
    list_filter = ("country", "is_active")
    search_fields = ("name", "slug", "city", "phone", "email")
    readonly_fields = ("slug", "patient_counter", "created_at")
    fieldsets = (
        (None, {"fields": ("name", "slug", "is_active", "country", "timezone")}),
        ("Contact", {"fields": ("address", "city", "phone", "email")}),
        (
            "Reminders & appointments",
            {
                "fields": (
                    "appointment_reminder_days",
                    "followup_reminder_days",
                    "overdue_grace_days",
                    "default_appointment_minutes",
                )
            },
        ),
        ("Printed prescriptions", {"fields": ("prescription_header", "prescription_footer")}),
        ("Records", {"fields": ("patient_counter", "created_at")}),
    )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "clinic", "role", "title", "is_active", "created_at")
    list_filter = ("role", "is_active")
    search_fields = ("user__email", "user__full_name", "clinic__name")
    autocomplete_fields = ["user", "clinic"]
    list_select_related = ("user", "clinic")
