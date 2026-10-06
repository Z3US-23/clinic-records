"""Signing in and out, clinic sign-up, your profile, staff and clinic settings."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.db.models import Case, IntegerField, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from django.views.generic import FormView, ListView, TemplateView, UpdateView

from apps.core.audit import Action, log_action
from apps.core.middleware import CurrentClinicMiddleware
from apps.core.permissions import ClinicRequiredMixin, ClinicScopedMixin, OwnerRequiredMixin

from .forms import (
    ChangePasswordForm,
    ClinicSettingsForm,
    ClinicSignupForm,
    EmailAuthenticationForm,
    PrescriptionDetailsForm,
    ProfileForm,
    StaffAddForm,
    StaffEditForm,
    StaffSetPasswordForm,
)
from .models import Clinic, Membership, User

Role = Membership.Role
CLINIC_SESSION_KEY = CurrentClinicMiddleware.SESSION_KEY


# --- Signing in and out -----------------------------------------------------


class SignInView(auth_views.LoginView):
    """Email + password sign-in. Only a safe ?next= is followed (Django checks the host)."""

    form_class = EmailAuthenticationForm
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["allow_signup"] = settings.ALLOW_CLINIC_SIGNUP
        return context


class SignOutView(auth_views.LogoutView):
    """POST only (Django refuses GET with 405)."""

    next_page = "accounts:login"

    def post(self, request, *args, **kwargs):
        was_signed_in = request.user.is_authenticated
        response = super().post(request, *args, **kwargs)
        if was_signed_in:
            messages.info(request, "You have signed out.")
        return response


class PasswordChangeView(auth_views.PasswordChangeView):
    form_class = ChangePasswordForm
    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("accounts:password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)  # saves and keeps this session signed in
        log_action(self.request, Action.UPDATE, self.request.user, "Changed own password")
        return response


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"


# --- New clinic sign-up -----------------------------------------------------


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class SignupView(FormView):
    form_class = ClinicSignupForm
    template_name = "accounts/signup.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ALLOW_CLINIC_SIGNUP:
            raise Http404("Clinic sign-up is closed.")
        if request.user.is_authenticated:
            messages.info(request, "You are already signed in. Sign out first to register another clinic.")
            return redirect("core:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            clinic, user = form.save()
        except IntegrityError:
            # Someone registered the same email a moment ago.
            form.add_error("email", "An account with this email already exists. Please sign in instead.")
            return self.form_invalid(form)

        login(self.request, user)
        self.request.session[CLINIC_SESSION_KEY] = clinic.pk
        log_action(self.request, Action.CREATE, clinic, "Clinic registered", clinic=clinic, user=user)
        messages.success(
            self.request,
            f"Welcome, {user.full_name}! {clinic.name} is ready. Start by adding your first patient.",
        )
        return redirect("core:dashboard")


# --- Signed in but not part of any clinic -----------------------------------


class NoClinicView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/no_clinic.html"

    def get(self, request, *args, **kwargs):
        if request.clinic is not None:
            return redirect("core:dashboard")
        return super().get(request, *args, **kwargs)


@login_required
@require_POST
def switch_clinic(request, clinic_id):
    """Work in another clinic the user belongs to (only clinics where they are active)."""
    membership = get_object_or_404(
        Membership.objects.select_related("clinic"),
        user=request.user,
        clinic_id=clinic_id,
        is_active=True,
        clinic__is_active=True,
    )
    request.session[CLINIC_SESSION_KEY] = membership.clinic_id
    messages.success(request, f"You are now working in {membership.clinic.name}.")
    return redirect("core:dashboard")


# --- Your profile -----------------------------------------------------------


class ProfileView(ClinicRequiredMixin, View):
    """Everyone edits their name; doctors and owners also edit what prints on prescriptions."""

    template_name = "accounts/profile.html"

    def get_forms(self, data=None):
        request = self.request
        # Fresh copies, so a form with errors doesn't change the name shown in the sidebar.
        user = User.objects.get(pk=request.user.pk)
        profile_form = ProfileForm(data, instance=user)
        details_form = None
        if request.membership.is_clinician:
            membership = Membership.objects.get(pk=request.membership.pk)
            details_form = PrescriptionDetailsForm(data, instance=membership)
        return profile_form, details_form

    def render_page(self, profile_form, details_form):
        return render(
            self.request,
            self.template_name,
            {"profile_form": profile_form, "details_form": details_form},
        )

    def get(self, request, *args, **kwargs):
        return self.render_page(*self.get_forms())

    def post(self, request, *args, **kwargs):
        profile_form, details_form = self.get_forms(request.POST)
        forms_valid = profile_form.is_valid() and (details_form is None or details_form.is_valid())
        if not forms_valid:
            return self.render_page(profile_form, details_form)

        profile_form.save()
        if details_form is not None:
            details_form.save()
        log_action(request, Action.UPDATE, request.user, "Updated own profile")
        messages.success(request, "Your profile has been saved.")
        return redirect("accounts:profile")


# --- Staff (clinic owner only) ----------------------------------------------


class StaffListView(OwnerRequiredMixin, ListView):
    template_name = "accounts/staff_list.html"
    context_object_name = "memberships"

    def get_queryset(self):
        role_order = Case(
            When(role=Role.OWNER, then=0),
            When(role=Role.DOCTOR, then=1),
            default=2,
            output_field=IntegerField(),
        )
        return (
            Membership.objects.filter(clinic=self.request.clinic)
            .select_related("user")
            .order_by("-is_active", role_order, "user__full_name")
        )


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class StaffAddView(OwnerRequiredMixin, FormView):
    form_class = StaffAddForm
    template_name = "accounts/staff_add.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["clinic"] = self.request.clinic
        return kwargs

    def form_valid(self, form):
        try:
            membership, created = form.save()
        except IntegrityError:
            # The same person was added (or registered) a moment ago.
            form.add_error("email", "This person was just added. Please check the staff list.")
            return self.form_invalid(form)

        user = membership.user
        log_action(
            self.request,
            Action.CREATE,
            membership,
            f"Added staff member {user.full_name} ({membership.get_role_display()})",
        )
        if created:
            messages.success(
                self.request,
                f"{user.full_name} has been added. Give them their email and password so they can sign in.",
            )
        else:
            messages.info(
                self.request,
                f"{user.full_name} already had an account and can sign in with their existing password.",
            )
        return redirect("accounts:staff_list")


class StaffEditView(OwnerRequiredMixin, ClinicScopedMixin, UpdateView):
    model = Membership
    form_class = StaffEditForm
    template_name = "accounts/staff_edit.html"
    context_object_name = "membership"

    def get_queryset(self):
        return super().get_queryset().select_related("user")

    def form_valid(self, form):
        membership = form.save()
        if form.has_changed():
            log_action(
                self.request,
                Action.UPDATE,
                membership,
                f"Updated staff member {membership.user.full_name} ({', '.join(form.changed_data)})",
            )
        messages.success(self.request, f"Saved changes for {membership.user.full_name}.")

        # An owner who just made themselves a non-owner can no longer see the staff pages.
        if membership.pk == self.request.membership.pk and not (membership.is_active and membership.is_owner):
            return redirect("core:dashboard")
        return redirect("accounts:staff_list")


@method_decorator(sensitive_post_parameters("new_password1", "new_password2"), name="dispatch")
class StaffSetPasswordView(OwnerRequiredMixin, FormView):
    """An owner sets a new password for someone who works ONLY at this clinic.

    If the person also works at another clinic, their account is not this
    owner's to control, so we refuse (403) and explain why.
    """

    form_class = StaffSetPasswordForm
    template_name = "accounts/staff_set_password.html"

    @cached_property
    def membership(self):
        return get_object_or_404(
            Membership.objects.select_related("user"), pk=self.kwargs["pk"], clinic=self.request.clinic
        )

    def refusal(self):
        """A response explaining why the password can't be set here, or None if it can."""
        person = self.membership.user
        if person == self.request.user:
            messages.info(self.request, "To change your own password, enter your current password here.")
            return redirect("accounts:password_change")

        works_elsewhere = Membership.objects.filter(user=person).exclude(clinic=self.request.clinic).exists()
        if works_elsewhere or person.is_staff or person.is_superuser:
            return render(
                self.request,
                "accounts/staff_password_blocked.html",
                {"membership": self.membership, "works_elsewhere": works_elsewhere},
                status=403,
            )
        return None

    def get(self, request, *args, **kwargs):
        return self.refusal() or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.refusal() or super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.membership.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["membership"] = self.membership
        return context

    def form_valid(self, form):
        person = form.save()  # also signs them out of other devices (their old sessions stop working)
        log_action(self.request, Action.UPDATE, person, f"Reset password for {person.full_name}")
        messages.success(
            self.request,
            f"New password saved for {person.full_name}. Tell them the new password privately.",
        )
        return redirect("accounts:staff_list")


# --- Clinic settings (clinic owner only) ------------------------------------


class ClinicSettingsView(OwnerRequiredMixin, UpdateView):
    form_class = ClinicSettingsForm
    template_name = "accounts/clinic_settings.html"
    success_url = reverse_lazy("accounts:clinic_settings")

    def get_object(self, queryset=None):
        # A fresh copy, so a form with errors doesn't change the clinic name shown in the page header.
        return Clinic.objects.get(pk=self.request.clinic.pk)

    def form_valid(self, form):
        clinic = form.save()
        if form.has_changed():
            log_action(
                self.request,
                Action.UPDATE,
                clinic,
                f"Updated clinic settings ({', '.join(form.changed_data)})",
            )
        messages.success(self.request, "Clinic settings saved.")
        return redirect(self.success_url)
