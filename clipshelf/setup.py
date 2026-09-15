"""First-run setup wizard: first visitor on an empty database creates the admin."""

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.shortcuts import redirect, render

from clipshelf.models import User

# ponytail: module-level cached flag; users are disabled, never deleted, so the
# count can never return to zero once it leaves it — one-way latch is safe.
_setup_done = False


def needs_setup() -> bool:
    """True while no user row exists; cached once the first user appears."""
    global _setup_done
    if _setup_done:
        return False
    if User.objects.exists():
        _setup_done = True
        return False
    return True


class SetupMiddleware:
    """While setup is pending, funnel every request to the wizard."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if needs_setup() and not (
            request.path == "/setup"
            or request.path == "/healthz"
            or request.path.startswith("/static/")
        ):
            return redirect("clipshelf_setup")
        return self.get_response(request)


def setup_view(request):
    if not needs_setup():
        return redirect(settings.LOGIN_URL)

    errors = []
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")
        try:
            EmailValidator()(email)
        except ValidationError:
            errors.append("Enter a valid email address.")
        if password1 != password2:
            errors.append("Passwords do not match.")
        if not errors:
            try:
                validate_password(password1)
            except ValidationError as exc:
                errors.extend(exc.messages)
        if not errors:
            with transaction.atomic():
                # ponytail: exists() re-check inside the transaction guards a
                # concurrent second POST; SQLite serializes writers.
                if User.objects.exists():
                    return redirect(settings.LOGIN_URL)
                from clipshelf.accounts import create_admin  # lazy: avoid cycle

                # Verified: whoever reaches an unconfigured server on first boot
                # is the machine's operator; also mandatory —
                # ACCOUNT_EMAIL_VERIFICATION is "mandatory" and accounts.api_user
                # rejects unverified accounts, so an unverified admin is locked
                # out of the API.
                user, _ = create_admin(email, password1, verified=True)
            login(
                request,
                user,
                backend="allauth.account.auth_backends.AuthenticationBackend",
            )
            return redirect("/")
    else:
        email = ""

    return render(
        request,
        "clipshelf/setup.html",
        {"email": email, "errors": errors},
    )
