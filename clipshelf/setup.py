"""First-run setup wizard: first visitor on an empty database creates the admin."""

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.csrf import csrf_failure as django_csrf_failure

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


# Enforces the token check even without CsrfViewMiddleware, which only runs
# when CLIPSHELF_CSRF=1; /setup must not accept cross-site POSTs by default.
@csrf_protect
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
                # user carries the email so UserAttributeSimilarityValidator
                # rejects a password equal or similar to it, matching the
                # allauth signup path.
                validate_password(password1, user=User(email=email))
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


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    """Rejected wizard POSTs name the CLIPSHELF_ORIGIN remedy.

    Behind a TLS-terminating reverse proxy the browser's Origin is https://
    while the app still sees http, so the first-visit form dies with Django's
    generic "Origin checking failed" page. The rejection is unchanged: the
    same checks run and the answer is still 403; once setup is done this
    falls back to Django's default page.
    """
    if not needs_setup():
        return django_csrf_failure(request, reason, template_name)
    return render(
        request,
        "clipshelf/setup.html",
        {
            "email": "",
            "errors": [
                "Setup could not verify this request: the browser's Origin "
                "does not match this server's address (CSRF check). Reach the "
                "server directly, or set CLIPSHELF_ORIGIN to the address shown "
                "in the browser (for example CLIPSHELF_ORIGIN=https://nas.example), "
                "restart, and reload this page."
            ],
        },
        status=403,
    )
