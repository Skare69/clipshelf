"""Clipshelf account surface.

Invitation-only admission on top of django-allauth, native API authentication
via the maintained allauth session-token strategy, and the browser invitation
entry link. Admin JSON endpoints live in clipshelf.admin_views.
"""

import hashlib
import re
import time
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import get_user as django_get_user
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.shortcuts import redirect
from django.urls import include, path, reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext_lazy as _
from django.conf import settings as django_settings
from urllib.parse import urlsplit, urlunsplit

from allauth.account import app_settings as account_settings
from allauth.account.adapter import DefaultAccountAdapter, get_adapter
from allauth.account.forms import SignupForm
from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress
from allauth.headless import app_settings as headless_settings
from allauth.headless.constants import Client
from allauth.core import context as allauth_context

from clipshelf.models import Invitation

# How long an administrator-issued invitation may be redeemed.
INVITATION_TTL = timedelta(days=7)

SESSION_INVITATION_KEY = "clipshelf_invitation_token"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def invitation_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _pending_invitation(request) -> Invitation | None:
    raw = request.session.get(SESSION_INVITATION_KEY)
    if not raw:
        return None
    return Invitation.objects.filter(
        token_hash=invitation_token_hash(raw),
        used_by__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()


def _headless_client(request) -> str | None:
    try:
        return request.allauth.headless.client
    except AttributeError:
        return None


class InviteSignupForm(SignupForm):
    """Signup form enforcing the exact invited mailbox at form level.

    The transactional consumption in AccountAdapter.save_user stays the
    authority; this only turns mismatched mailboxes into a clean form error
    instead of a late failure.
    """

    def clean_email(self):
        email = super().clean_email()
        # allauth does not hand the request to signup forms; use its own
        # request context so the mismatch is a form error, not a late 500.
        request = getattr(self, "request", None) or allauth_context.request
        if request is not None and not request.user.is_authenticated:
            invitation = _pending_invitation(request)
            if invitation is None or invitation.email.lower() != email.lower():
                raise ValidationError(
                    str(get_adapter().error_messages["invitation"])
                )
        return email


class AccountAdapter(DefaultAccountAdapter):
    """Allauth adapter implementing invitation-only admission.

    The invitation never proves mailbox ownership; the mandatory allauth
    email verification of the exact invited address still runs after signup.
    """

    error_messages = {
        **DefaultAccountAdapter.error_messages,
        "invitation": _(
            "Signup requires a valid invitation for this exact email address."
        ),
    }

    def is_open_for_signup(self, request):
        # Native headless signup is denied outright; onboarding happens in the
        # browser through GET /invite/<token> followed by framework signup.
        if _headless_client(request) == Client.APP:
            return False
        return _pending_invitation(request) is not None


    def save_user(self, request, user, form, commit=True):
        if not commit:
            return super().save_user(request, user, form, commit)
        email = form.cleaned_data.get("email") or ""
        with transaction.atomic():
            saved = super().save_user(request, user, form, commit=True)
            raw = request.session.get(SESSION_INVITATION_KEY)
            consumed = Invitation.objects.filter(
                token_hash=invitation_token_hash(raw or ""),
                used_by__isnull=True,
                expires_at__gt=timezone.now(),
                email__iexact=email,
            ).update(used_by=saved, used_at=timezone.now())
            if not consumed:
                # Lost a redemption race or lost the session stash: fail the
                # signup atomically, leaving no account behind.
                raise self.validation_error("invitation")
        request.session.pop(SESSION_INVITATION_KEY, None)
        return saved

    # -- links always come from the configured origin, never a Host header ----
    def _origin(self, url):
        origin = getattr(django_settings, "CLIPSHELF_ORIGIN", "") or ""
        if not origin:
            return url
        base = urlsplit(origin)
        parts = urlsplit(url)
        return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

    def get_reset_password_from_key_url(self, key):
        return self._origin(super().get_reset_password_from_key_url(key))

    def get_email_confirmation_url(self, request, emailconfirmation):
        return self._origin(super().get_email_confirmation_url(request, emailconfirmation))

    def send_mail(self, template_prefix, email, context):
        # SMTP failures must stay observable: never swallow them into a fake
        # success page.
        super().send_mail(template_prefix, email, context)


def invite_view(request, token):
    """GET /invite/<token>: begin the browser invitation flow."""
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    invitation = Invitation.objects.filter(
        token_hash=invitation_token_hash(token),
        used_by__isnull=True,
        expires_at__gt=timezone.now(),
    ).first()
    if invitation is None or get_user_model().objects.filter(
        email__iexact=invitation.email
    ).exists():
        messages.error(request, _("This invitation link is no longer valid."))
        return redirect("account_login")
    request.session[SESSION_INVITATION_KEY] = token
    return redirect(
        f"{reverse('account_signup')}?{urlencode({'email': invitation.email})}"
    )


def api_user(request):
    """Return the active, email-verified User for this request.

    Requests carrying X-Session-Token authenticate exclusively through the
    allauth session-token strategy (session-key token) plus Django's own
    session validation (auth hash incl. session_version and fallback
    secrets); a missing or invalid token never falls back to the browser
    cookie session. Raises PermissionDenied when unauthenticated.
    """
    strategy = headless_settings.TOKEN_STRATEGY
    token = strategy.get_session_token(request)
    if token:
        session = strategy.lookup_session(token)
        if session is None:
            raise PermissionDenied
        original = request.session
        request.session = session
        try:
            user = django_get_user(request)
        finally:
            # Restore the browser session so SessionMiddleware never leaks
            # the token session as a Set-Cookie.
            request.session = original
            request.META["CSRF_COOKIE_NEEDS_UPDATE"] = False
    else:
        user = request.user
    if not user.is_authenticated or not user.is_active:
        raise PermissionDenied
    if not EmailAddress.objects.filter(
        user=user, email__iexact=user.email, verified=True
    ).exists():
        raise PermissionDenied
    return user


def create_admin(email, password=None, *, verified=False):
    """Create or promote the application administrator for `email`.

    Returns (user, created). The user always ends up with
    ``is_app_admin=True`` and a primary EmailAddress row for the address;
    `password` sets a usable password, `verified` marks the address verified.
    """
    User = get_user_model()
    with transaction.atomic():
        user = User.objects.filter(email__iexact=email).first()
        created = False
        if user is None:
            base = re.sub(r"[^a-z0-9_.-]+", "", email.split("@")[0].lower()) or "clipshelf"
            username, n = base, 1
            while User.objects.filter(username__iexact=username).exists():
                username = f"{base}-{n}"
                n += 1
            user = User.objects.create_user(username=username, email=email)
            created = True  # unusable password: operator or user sets it via the framework
        user.is_app_admin = True
        user.save(update_fields=["is_app_admin"])
        address = EmailAddress.objects.filter(user=user).first()
        if address is None:
            address = EmailAddress(user=user, email=email, primary=True, verified=False)
            address.save()
        if password is not None:
            user.set_password(password)
            user.save(update_fields=["password"])
        if verified:
            address.verified = True
            address.save()
    return user, created


_csrf_middleware = CsrfViewMiddleware(lambda request: None)


def api_csrf(view_func):
    """Central CSRF entry for API views.

    CSRF protection is opt-in (``CLIPSHELF_CSRF=1``): the default
    deployment is plain HTTP on a LAN behind a VPN, and
    ``SESSION_COOKIE_SAMESITE="Lax"`` is what keeps cookie mutations safe
    by default. When enabled, the csrf_exempt view's unsafe requests
    WITHOUT an X-Session-Token header are run through Django's maintained
    CSRF verification. Token requests rely on the header being a valid
    session token, enforced by api_user.
    """

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if (
            request.method not in _SAFE_METHODS
            and not request.headers.get("x-session-token")
            and getattr(django_settings, "CLIPSHELF_CSRF", False)
        ):
            rejected = _csrf_middleware.process_view(request, view_func, args, kwargs)
            if rejected is not None:
                return JsonResponse({"detail": "CSRF verification failed."}, status=403)
        return view_func(request, *args, **kwargs)

    wrapped.csrf_exempt = True
    return wrapped


def json_error(status, detail=None, errors=None):
    payload = {}
    if detail:
        payload["detail"] = detail
    if errors:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


class ApiError(Exception):
    """Carries a ready-to-send JSON error response through admin views."""

    def __init__(self, status, detail=None, errors=None):
        super().__init__(detail)
        self.response = json_error(status, detail=detail, errors=errors)


def has_recent_reauthentication(request) -> bool:
    """True when the current session has a framework authentication record
    within allauth's REAUTHENTICATION_TIMEOUT."""
    timeout = account_settings.REAUTHENTICATION_TIMEOUT
    now = time.time()
    return any(
        record.get("at", 0) >= now - timeout
        for record in request.session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
    )


account_urlpatterns = [
    path("accounts/", include("allauth.account.urls")),
    path("_allauth/", include("allauth.headless.urls")),
    path("invite/<str:token>", invite_view, name="clipshelf_invite"),
]
