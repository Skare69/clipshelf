"""Clipshelf app-admin JSON API.

App administration never implies content access and never maps to the stock
Django superuser: these endpoints only manage identity access (accounts,
invitations, shared-collection succession, LLM connection settings).
"""

import json
import os
import secrets
from functools import wraps

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F
from django.http import JsonResponse
from django.urls import path, reverse
from django.utils import timezone

from allauth.account import app_settings as account_settings
from allauth.account.adapter import get_adapter
from allauth.account.internal.flows.login import record_authentication
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str

from clipshelf.accounts import (
    INVITATION_TTL,
    ApiError,
    api_csrf,
    api_user,
    has_recent_reauthentication,
    invitation_token_hash,
    json_error,
)
from clipshelf import judgment
from clipshelf.models import Collection, Invitation, Membership
from clipshelf.services import get_settings, screening_key

_USER = get_user_model()


def _json_body(request) -> dict:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise ApiError(400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise ApiError(400, detail="invalid JSON body")
    return body


def _require_reauth(request, body) -> None:
    """Sensitive admin actions need recent framework reauthentication, or the
    admin's current password in reauth_password."""
    if has_recent_reauthentication(request):
        return
    password = body.get("reauth_password")
    if password and request.user.check_password(password):
        # Mirror allauth's own reauthentication bookkeeping so follow-up
        # sensitive calls inside the timeout window need no password.
        record_authentication(
            request, request.user, method="password", reauthenticated=True
        )
        return
    raise ApiError(403, detail="reauthentication_required")


def admin_endpoint(*methods):
    """Wrap an admin view: conditional CSRF, api_user auth, is_app_admin gate,
    and JSON error mapping."""

    def decorate(view):
        @api_csrf
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return json_error(405, detail="method_not_allowed")
            try:
                user = api_user(request)
            except PermissionDenied:
                return json_error(401, detail="unauthenticated")
            if not user.is_app_admin:
                return json_error(403, detail="forbidden")
            try:
                return view(request, user, *args, **kwargs)
            except ApiError as exc:
                return exc.response
            except ValidationError as exc:
                return json_error(400, errors=[str(m) for m in exc.messages])

        return wrapped

    return decorate


def _verified_ids(users) -> set:
    return set(
        EmailAddress.objects.filter(verified=True, user__in=users).values_list(
            "user_id", flat=True
        )
    )


def _user_dict(user, verified: set) -> dict:
    return {
        "id": str(user.pk),
        "email": user.email,
        "is_app_admin": user.is_app_admin,
        "is_active": user.is_active,
        "is_verified": user.pk in verified,
        "date_joined": user.date_joined.isoformat(),
    }


@admin_endpoint("GET")
def user_list(request, user):
    users = list(_USER.objects.order_by("email"))
    verified = _verified_ids(users)
    return JsonResponse({"users": [_user_dict(u, verified) for u in users]})


def _target_or_404(user_id):
    target = _USER.objects.filter(pk=user_id).first()
    if target is None:
        raise ApiError(404, detail="no such user")
    return target


@admin_endpoint("POST")
def user_status(request, user, user_id):
    """Reversible disable/enable. Disabling revokes existing sessions; later
    re-enabling never revives revoked sessions."""
    body = _json_body(request)
    _require_reauth(request, body)
    active = body.get("active")
    if not isinstance(active, bool):
        raise ApiError(400, detail="active must be a boolean")
    target = _target_or_404(user_id)
    with transaction.atomic():
        if target.is_active and not active:
            if target.pk == user.pk:
                raise ApiError(400, detail="You cannot disable your own account.")
            target.is_active = False
            target.session_version = F("session_version") + 1
            target.save(update_fields=["is_active", "session_version"])
        elif active and not target.is_active:
            target.is_active = True
            target.save(update_fields=["is_active"])
    target.refresh_from_db()
    return JsonResponse({"user": _user_dict(target, _verified_ids([target]))})


@admin_endpoint("POST")
def user_recover(request, user, user_id):
    """Issue a maintained allauth/Django reset link for the SAME account,
    revoking its existing sessions. Enrolled MFA is never touched; the
    administrator gains no collection membership or content access."""
    _require_reauth(request, _json_body(request))
    target = _target_or_404(user_id)
    with transaction.atomic():
        _USER.objects.filter(pk=target.pk).update(
            session_version=F("session_version") + 1
        )
    temp_key = account_settings.PASSWORD_RESET_TOKEN_GENERATOR().make_token(target)
    key = f"{user_pk_to_url_str(target)}-{temp_key}"
    reset_url = get_adapter(request).get_reset_password_from_key_url(key)
    return JsonResponse({"reset_url": reset_url})


def _invitation_dict(invitation: Invitation) -> dict:
    return {
        "id": str(invitation.pk),
        "email": invitation.email,
        "created_by_id": (
            str(invitation.created_by_id) if invitation.created_by_id else None
        ),
        "created_at": (
            invitation.created_at.isoformat() if invitation.created_at else None
        ),
        "expires_at": invitation.expires_at.isoformat(),
        "used_at": invitation.used_at.isoformat() if invitation.used_at else None,
        "used_by_id": str(invitation.used_by_id) if invitation.used_by_id else None,
    }


@admin_endpoint("GET", "POST")
def invitations(request, user):
    if request.method == "GET":
        rows = Invitation.objects.order_by("-created_at")
        return JsonResponse({"invitations": [_invitation_dict(r) for r in rows]})
    body = _json_body(request)
    _require_reauth(request, body)
    email = str(body.get("email") or "").strip().lower()
    if len(email) > 254:
        raise ApiError(400, detail="A valid email address is required.")
    try:
        validate_email(email)
    except ValidationError:
        raise ApiError(400, detail="A valid email address is required.")
    if _USER.objects.filter(email=email).exists():
        raise ApiError(400, detail="That email is already registered.")
    live = Invitation.objects.filter(
        email__iexact=email, used_by__isnull=True, expires_at__gt=timezone.now()
    )
    if live.exists():
        raise ApiError(409, detail="An unused invitation for that email already exists.")
    token = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(
        email=email,
        token_hash=invitation_token_hash(token),
        created_by=user,
        expires_at=timezone.now() + INVITATION_TTL,
    )
    data = _invitation_dict(invitation)
    # The copyable link exists exactly once: raw tokens are never stored.
    data["url"] = request.build_absolute_uri(
        reverse("clipshelf_invite", args=[token])
    )
    return JsonResponse({"invitation": data}, status=201)


@admin_endpoint("GET")
def collection_list(request, user):
    """Shared-collection ownership and member metadata only; never Personal
    collections and never content."""
    collections = (
        Collection.objects.filter(kind="shared")
        .select_related("owner")
        .order_by("name")
    )
    members: dict = {}
    for membership in Membership.objects.filter(
        collection__kind="shared"
    ).select_related("user"):
        members.setdefault(membership.collection_id, []).append(membership.user)
    data = [
        {
            "id": str(c.pk),
            "name": c.name,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "owner": {
                "id": str(c.owner_id),
                "email": c.owner.email,
                "is_active": c.owner.is_active,
            },
            "members": [
                {"id": str(u.pk), "email": u.email, "is_active": u.is_active}
                for u in members.get(c.pk, [])
            ],
        }
        for c in collections
    ]
    return JsonResponse({"collections": data})


@admin_endpoint("POST")
def collection_transfer(request, user, collection_id):
    """Move a disabled owner's shared collection to an existing active member.
    Grants the acting admin neither membership nor content access; Personal
    collections are never transferable."""
    body = _json_body(request)
    _require_reauth(request, body)
    collection = Collection.objects.filter(pk=collection_id).first()
    if collection is None:
        raise ApiError(404, detail="no such collection")
    if collection.kind != "shared":
        raise ApiError(400, detail="Personal collections cannot be transferred.")
    if collection.owner.is_active:
        raise ApiError(
            400, detail="Only a disabled owner's collection can be transferred."
        )
    recipient = _USER.objects.filter(pk=body.get("owner_id"), is_active=True).first()
    if recipient is None:
        raise ApiError(400, detail="Recipient must be an existing active user.")
    if not Membership.objects.filter(collection=collection, user=recipient).exists():
        raise ApiError(
            400, detail="Recipient must be an existing member of the collection."
        )
    collection.owner = recipient
    collection.save(update_fields=["owner"])
    return JsonResponse(
        {
            "collection": {
                "id": str(collection.pk),
                "name": collection.name,
                "owner": {
                    "id": str(recipient.pk),
                    "email": recipient.email,
                    "is_active": recipient.is_active,
                },
            }
        }
    )


def _llm_dict(s) -> dict:
    return {
        "base_url": s.llm_base_url or "",
        "model": s.llm_model or "",
        "concurrency": s.llm_concurrency,
        "has_api_key": bool(s.llm_api_key),
        "verified_at": s.llm_verified_at.isoformat() if s.llm_verified_at else None,
    }


def _resolve_kwargs(body) -> dict:
    """Present-only request fields for ServerSettings.resolve. Omission must
    stay distinct from an explicit empty value, which clears the key."""
    kwargs = {}
    if "base_url" in body:
        kwargs["base_url"] = str(body["base_url"] or "").strip()
    if "model" in body:
        kwargs["model"] = str(body["model"] or "").strip()
    if "api_key" in body:
        kwargs["api_key"] = str(body["api_key"] or "")
    return kwargs


@admin_endpoint("GET", "POST")
def llm_config(request, user):
    s = get_settings()
    if request.method == "GET":
        return JsonResponse({"llm": _llm_dict(s)})
    body = _json_body(request)
    _require_reauth(request, body)
    if "base_url" in body:
        base_url = str(body["base_url"] or "").strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ApiError(400, detail="base_url must be an http(s) URL.")
    if "concurrency" in body:
        try:
            concurrency = int(body["concurrency"])
        except (TypeError, ValueError):
            raise ApiError(400, detail="concurrency must be an integer.")
        if not 1 <= concurrency <= 8:
            raise ApiError(400, detail="concurrency must be between 1 and 8.")
        s.llm_concurrency = concurrency
    base_url, model, api_key = s.resolve(**_resolve_kwargs(body))
    s.llm_base_url, s.llm_model, s.llm_api_key = base_url, model, api_key or ""
    s.save()  # verification reset and endpoint key rules live on the model
    return JsonResponse({"llm": _llm_dict(s)})


@admin_endpoint("POST")
def llm_check(request, user):
    """Probe the values on screen; omission falls back to the saved ones.
    Pure preview: typed values are never persisted."""
    body = _json_body(request)
    _require_reauth(request, body)
    base_url, model, api_key = get_settings().resolve(**_resolve_kwargs(body))
    from clipshelf.interpretation import check_connection
    return JsonResponse(
        {"check": check_connection({"base_url": base_url, "model": model, "api_key": api_key})}
    )


@admin_endpoint("POST")
def llm_models(request, user):
    """List what the endpoint offers, using the typed base URL/key so the admin
    can pick a model before committing the settings. Pure preview."""
    body = _json_body(request)
    _require_reauth(request, body)
    base_url, _model, api_key = get_settings().resolve(**_resolve_kwargs(body))
    if not base_url:
        raise ApiError(400, detail="Enter a base URL first.")
    from clipshelf.interpretation import ConfigurationError, list_models

    try:
        models = list_models({"base_url": base_url, "api_key": api_key})
    except ConfigurationError as exc:
        raise ApiError(502, detail=str(exc))
    return JsonResponse({"models": models})


def _screening_dict() -> dict:
    return {
        "has_api_key": bool(get_settings().typesafe_api_key),
        "env_fallback": bool(os.environ.get("TYPESAFE_API_KEY", "").strip()),
    }


@admin_endpoint("GET", "POST")
def screening(request, user):
    """TypeSafe screening key. Write-only like llm_api_key: never echoed."""
    s = get_settings()
    if request.method == "GET":
        return JsonResponse({"screening": _screening_dict()})
    body = _json_body(request)
    _require_reauth(request, body)
    if "api_key" not in body:
        raise ApiError(400, detail="api_key is required")
    s.typesafe_api_key = str(body["api_key"])
    s.save(update_fields=["typesafe_api_key"])
    return JsonResponse({"screening": _screening_dict()})


@admin_endpoint("POST")
def screening_check(request, user):
    """One tiny real noul round-trip with the stored (or env) key."""
    body = _json_body(request)
    _require_reauth(request, body)
    key = screening_key()
    if not judgment.available(key):
        return JsonResponse(
            {"ok": False, "message": "screening unavailable: no API key set"})
    try:
        p = judgment.ping(key)
    except judgment.JudgmentError as exc:
        message = str(exc)
        if key:
            message = message.replace(key, "***")
        return JsonResponse({"ok": False, "message": message[:300]})
    return JsonResponse({"ok": True, "message": f"screening ok (p={p:.2f})"})


admin_urlpatterns = [
    path("admin/users", user_list, name="clipshelf_admin_users"),
    path(
        "admin/users/<uuid:user_id>/status", user_status,
        name="clipshelf_admin_user_status",
    ),
    path(
        "admin/users/<uuid:user_id>/recover", user_recover,
        name="clipshelf_admin_user_recover",
    ),
    path("admin/invitations", invitations, name="clipshelf_admin_invitations"),
    path("admin/collections", collection_list, name="clipshelf_admin_collections"),
    path(
        "admin/collections/<uuid:collection_id>/transfer", collection_transfer,
        name="clipshelf_admin_collection_transfer",
    ),
    path("admin/llm", llm_config, name="clipshelf_admin_llm"),
    path("admin/llm/check", llm_check, name="clipshelf_admin_llm_check"),
    path("admin/llm/models", llm_models, name="clipshelf_admin_llm_models"),
    path("admin/screening", screening, name="clipshelf_admin_screening"),
    path("admin/screening/check", screening_check,
         name="clipshelf_admin_screening_check"),
]
