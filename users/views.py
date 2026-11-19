import logging

from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.generic import View

from .forms import LoginForm, ProfileForm, RegisterForm

logger = logging.getLogger(__name__)


class LoginView(View):
    """
    Login view
    get(): Returns the login page with the login form
    post(): Authenticates the user and logs them in
    """

    def get(self, request, *args, **kwargs):
        form = LoginForm()
        return render(request, "users/login.html", {"form": form})

    def post(self, request, *args, **kwargs):
        form = LoginForm(request.POST)

        if form.is_valid():
            email = form.cleaned_data.get("email")
            password = form.cleaned_data.get("password")

            user = authenticate(request, email=email, password=password)

            if user is not None:
                login(request, user)
                logger.info(f"User {user.email} logged in")
                redirect_url = request.GET.get("next", "home")

                return redirect(redirect_url)
            logger.warning(f"Invalid login attempt for {email}")
            form.add_error(None, "Invalid email or password")

        logger.warning(f"Invalid login attempt: {form.errors}")

        return render(request, "users/login.html", {"form": form})


class RegisterView(View):
    """
    Register view
    get(): Returns the register page with the register form
    post(): Registers the user
    """

    def get(self, request, *args, **kwargs):
        form = RegisterForm()
        return render(request, "users/register.html", {"form": form})

    def post(self, request, *args, **kwargs):
        form = RegisterForm(request.POST)

        if form.is_valid():
            user = form.save(commit=False)
            password = form.cleaned_data.get("password")

            user.set_password(password)
            user.save()

            logger.info(f"User {user.email} registered")

            return redirect("login")
        logger.warning(f"Invalid registration attempt: {form.errors}")

        return render(request, "users/register.html", {"form": form})


class LogoutView(View):
    """
    Logout view
    get(): Logs the user out
    """

    def get(self, request, *args, **kwargs):
        logout(request)
        logger.info("User logged out")
        return redirect("login")


class ProfileView(LoginRequiredMixin, View):
    template_name = "users/profile.html"

    def get(self, request, *args, **kwargs):
        return render(
            request,
            self.template_name,
            {
                "profile_form": ProfileForm(instance=request.user),
                "password_form": PasswordChangeForm(request.user),
            },
        )

    def post(self, request, *args, **kwargs):
        active_form = request.POST.get("form_type", "profile")

        if active_form == "password":
            password_form = PasswordChangeForm(request.user, request.POST)
            profile_form = ProfileForm(instance=request.user)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Password changed successfully.")
                logger.info("User %s changed their password", request.user.email)
                return redirect("profile")

        else:
            profile_form = ProfileForm(request.POST, instance=request.user)
            password_form = PasswordChangeForm(request.user)

            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Profile updated successfully.")
                logger.info("User %s updated their profile", request.user.email)
                return redirect("profile")

        return render(
            request,
            self.template_name,
            {
                "profile_form": profile_form,
                "password_form": password_form,
            },
        )
