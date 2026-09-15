"""Focused Clipshelf account tests.

Invitation replay, native token login and session revocation, admin boundary
(never content access), and shared-collection transfer limits.
"""

import json
import secrets
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from allauth.account import app_settings as account_settings
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str

from clipshelf import accounts, admin_views
from clipshelf.accounts import (
    SESSION_INVITATION_KEY,
    api_user,
    invitation_token_hash,
    json_error,
)
from clipshelf.interpretation import ConfigurationError
from clipshelf.models import Collection, Invitation, Membership
from clipshelf.services import accessible_collections, get_settings
from django.core import mail

PASSWORD = "correct horse battery staple 42!"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "DIRS": [],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


def _probe(request):
    try:
        user = api_user(request)
    except PermissionDenied:
        return json_error(401, detail="unauthenticated")
    return JsonResponse({"id": str(user.pk)})


urlpatterns = [
    path("probe", _probe),
    path("admin/users", admin_views.user_list),
    path("admin/users/<uuid:user_id>/status", admin_views.user_status),
    path("admin/users/<uuid:user_id>/recover", admin_views.user_recover),
    path("admin/collections", admin_views.collection_list),
    path(
        "admin/collections/<uuid:collection_id>/transfer",
        admin_views.collection_transfer,
    ),
    path("admin/llm/models", admin_views.llm_models),
    path("invite/<str:token>", accounts.invite_view, name="clipshelf_invite"),
    path("accounts/", include("allauth.account.urls")),
    path("_allauth/", include("allauth.headless.urls")),
]


def make_user(email, *, password=PASSWORD, verified=True, **fields):
    user = get_user_model()(username=uuid4().hex, email=email.lower(), **fields)
    user.set_password(password)
    user.save()
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=verified
    )
    return user


def make_invitation(email, created_by, *, expires_at=None):
    token = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(
        email=email.lower(),
        token_hash=invitation_token_hash(token),
        created_by=created_by,
        expires_at=expires_at or timezone.now() + timedelta(days=1),
    )
    return token, invitation


@override_settings(ROOT_URLCONF="clipshelf.test_accounts", TEMPLATES=TEMPLATES)
class AccountTestCase(TestCase):
    def setUp(self):
        self.admin = make_user("admin@clipshelf.test", is_app_admin=True)
        self.client = Client()

    def headless_login(self, email, *, password=PASSWORD, client=None):
        client = client or self.client
        response = client.post(
            "/_allauth/app/v1/auth/login",
            data=json.dumps({"email": email, "password": password}),
            content_type="application/json",
        )
        return response

    def admin_post(self, url, body, *, reauth=False):
        payload = dict(body)
        if reauth:
            payload["reauth_password"] = PASSWORD
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )


class InvitationFlowTests(AccountTestCase):
    def test_invite_then_signup_consumes_invitation_and_requires_verification(self):
        token, invitation = make_invitation("invitee@clipshelf.test", self.admin)
        response = self.client.get(f"/invite/{token}")
        self.assertRedirects(
            response,
            f"{reverse('account_signup')}?email=invitee%40clipshelf.test",
            fetch_redirect_response=False,
        )
        self.assertIn(SESSION_INVITATION_KEY, self.client.session)

        response = self.client.post(
            reverse("account_signup"),
            data={
                "email": "invitee@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="invitee@clipshelf.test")
        invitation.refresh_from_db()
        self.assertEqual(invitation.used_by, user)
        self.assertIsNotNone(invitation.used_at)
        self.assertNotIn(SESSION_INVITATION_KEY, self.client.session)
        # Mandatory verified mailbox: the allauth confirmation email was sent.
        self.assertTrue(
            any("confirm" in message.subject.lower() for message in mail.outbox)
        )

    def test_replayed_invitation_cannot_create_second_account(self):
        token, invitation = make_invitation("once@clipshelf.test", self.admin)
        self.client.get(f"/invite/{token}")
        self.client.post(
            reverse("account_signup"),
            data={
                "email": "once@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        before = get_user_model().objects.count()

        # A stale session (or copied link) cannot redeem it twice.
        replay = Client()
        session = replay.session
        session[SESSION_INVITATION_KEY] = token
        session.save()
        response = replay.post(
            reverse("account_signup"),
            data={
                "email": "other@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.count(), before)
        invitation.refresh_from_db()
        self.assertEqual(invitation.used_by.email, "once@clipshelf.test")

    def test_non_invited_browser_and_headless_signup_fail(self):
        before = get_user_model().objects.count()
        response = self.client.post(
            reverse("account_signup"),
            data={
                "email": "walkin@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        # Signup closed: redirected to login or re-rendered with the error.
        self.assertIn(response.status_code, (200, 302))

        response = self.client.post(
            "/_allauth/app/v1/auth/signup",
            data=json.dumps(
                {"email": "headless@clipshelf.test", "password": PASSWORD}
            ),
            content_type="application/json",
        )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(get_user_model().objects.count(), before)

    def test_invitation_email_must_match_signup_email(self):
        token, _ = make_invitation("exact@clipshelf.test", self.admin)
        self.client.get(f"/invite/{token}")
        response = self.client.post(
            reverse("account_signup"),
            data={
                "email": "someone.else@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            get_user_model().objects.filter(
                email="someone.else@clipshelf.test"
            ).exists()
        )

    def test_api_user_denies_unverified_and_allows_verified(self):
        token, _ = make_invitation("verify@clipshelf.test", self.admin)
        self.client.get(f"/invite/{token}")
        self.client.post(
            reverse("account_signup"),
            data={
                "email": "verify@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        user = get_user_model().objects.get(email="verify@clipshelf.test")
        client = Client()
        client.force_login(user)
        response = client.get("/probe")
        self.assertEqual(response.status_code, 401)
        EmailAddress.objects.filter(user=user).update(verified=True)
        response = client.get("/probe")
        self.assertEqual(response.status_code, 200)


class NativeTokenTests(AccountTestCase):
    def test_invalid_token_header_never_falls_back_to_cookie(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/probe").status_code, 200)
        response = self.client.get(
            "/probe", headers={"x-session-token": "not-a-real-session"}
        )
        self.assertEqual(response.status_code, 401)

    def test_disable_revoke_and_reenable_does_not_revive_sessions(self):
        member = make_user("member@clipshelf.test")
        token = self.headless_login(member.email).json()["meta"]["session_token"]
        self.assertEqual(
            self.client.get("/probe", headers={"x-session-token": token}).status_code,
            200,
        )

        self.client.force_login(self.admin)
        response = self.admin_post(
            f"/admin/users/{member.pk}/status", {"active": False}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "reauthentication_required")

        response = self.admin_post(
            f"/admin/users/{member.pk}/status", {"active": False}, reauth=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/probe", headers={"x-session-token": token}).status_code,
            401,
        )

        # Re-enabling must not revive the revoked session; the recent
        # reauthentication from above means no password needed now.
        response = self.admin_post(f"/admin/users/{member.pk}/status", {"active": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/probe", headers={"x-session-token": token}).status_code,
            401,
        )
        # A fresh login works again.
        fresh = self.headless_login(member.email, client=Client())
        self.assertEqual(fresh.status_code, 200)
        new_token = fresh.json()["meta"]["session_token"]
        self.assertEqual(
            self.client.get(
                "/probe", headers={"x-session-token": new_token}
            ).status_code,
            200,
        )

    def test_recover_uses_framework_reset_token_and_revokes_sessions(self):
        target = make_user("lost@clipshelf.test")
        token = self.headless_login(target.email).json()["meta"]["session_token"]
        self.client.force_login(self.admin)

        response = self.admin_post(f"/admin/users/{target.pk}/recover", {})
        self.assertEqual(response.status_code, 403)

        response = self.admin_post(
            f"/admin/users/{target.pk}/recover", {}, reauth=True
        )
        self.assertEqual(response.status_code, 200)
        reset_url = response.json()["reset_url"]
        self.assertIn("/accounts/password/reset/key/", reset_url)
        uid_key = reset_url.split("/accounts/password/reset/key/")[1].strip("/")
        uid, _, key = uid_key.partition("-")
        self.assertEqual(uid, user_pk_to_url_str(target))
        target.refresh_from_db()  # login stamped last_login; the token hashes it
        self.assertTrue(
            account_settings.PASSWORD_RESET_TOKEN_GENERATOR().check_token(target, key)
        )
        self.assertEqual(
            self.client.get("/probe", headers={"x-session-token": token}).status_code,
            401,
        )

        # Recent reauthentication now suffices.
        target2 = make_user("lost2@clipshelf.test")
        response = self.admin_post(f"/admin/users/{target2.pk}/recover", {})
        self.assertEqual(response.status_code, 200)


class AdminBoundaryTests(AccountTestCase):
    def test_admin_endpoints_reject_anonymous_and_non_admins(self):
        self.assertEqual(self.client.get("/admin/users").status_code, 401)
        member = make_user("plain@clipshelf.test")
        client = Client()
        client.force_login(member)
        response = client.get("/admin/users")
        self.assertEqual(response.status_code, 403)
        response = client.post(
            f"/admin/users/{member.pk}/status", data={"active": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_role_grants_no_content_access(self):
        other = make_user("other@clipshelf.test")
        shared = Collection(name="Family", kind="shared", owner=other)
        shared.save()
        Membership(collection=shared, user=other).save()
        self.client.force_login(self.admin)
        response = self.client.get("/admin/collections")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["collections"]), 1)
        self.assertNotIn("entries", payload)
        # Admin sees metadata only and is not a member anywhere but Personal.
        self.assertEqual(accessible_collections(self.admin).count(), 1)

    def test_transfer_limits(self):
        owner = make_user("owner@clipshelf.test")
        member = make_user("member2@clipshelf.test")
        outsider = make_user("outsider@clipshelf.test")
        inactive = make_user("inactive@clipshelf.test", is_active=False)
        shared = Collection(name="House", kind="shared", owner=owner)
        shared.save()
        for user in (owner, member, inactive):
            Membership(collection=shared, user=user).save()
        personal = Collection.objects.get(owner=owner, kind="personal")
        url = f"/admin/collections/{shared.pk}/transfer"

        self.client.force_login(self.admin)
        # Personal collections never transfer.
        self.assertEqual(
            self.admin_post(
                f"/admin/collections/{personal.pk}/transfer",
                {"owner_id": str(member.pk)},
                reauth=True,
            ).status_code,
            400,
        )
        # Active owners cannot be displaced.
        self.assertEqual(
            self.admin_post(
                url, {"owner_id": str(member.pk)}, reauth=True
            ).status_code,
            400,
        )

        self.admin_post(f"/admin/users/{owner.pk}/status", {"active": False})

        self.assertEqual(
            self.admin_post(
                url, {"owner_id": str(outsider.pk)}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.admin_post(
                url, {"owner_id": str(inactive.pk)}
            ).status_code,
            400,
        )
        response = self.admin_post(url, {"owner_id": str(member.pk)})
        self.assertEqual(response.status_code, 200)
        shared.refresh_from_db()
        self.assertEqual(shared.owner, member)
        # The acting admin gained neither membership nor content access.
        self.assertFalse(
            Membership.objects.filter(collection=shared, user=self.admin).exists()
        )
        self.assertEqual(accessible_collections(self.admin).count(), 1)

        # Re-enabling the old owner does not undo the transfer.
        self.admin_post(f"/admin/users/{owner.pk}/status", {"active": True})
        shared.refresh_from_db()
        self.assertEqual(shared.owner, member)


class LlmModelPickerTests(AccountTestCase):
    """The admin picker must be usable before settings are committed, and must
    never fall back to a key the admin did not intend to use."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.seen = []

        def fake_list(config, **kw):
            self.seen.append(config)
            return ["vision-a", "vision-b"]

        patcher = patch("clipshelf.interpretation.list_models", fake_list)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_typed_key_is_used_before_the_settings_are_saved(self):
        response = self.admin_post(
            "/admin/llm/models",
            {"base_url": "http://endpoint/v1", "api_key": "typed"},
            reauth=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], ["vision-a", "vision-b"])
        self.assertEqual(self.seen[-1]["api_key"], "typed")

    def test_saved_key_and_base_url_fill_in_when_omitted(self):
        settings_row = get_settings()
        settings_row.llm_base_url = "http://saved/v1"
        settings_row.llm_api_key = "stored"
        settings_row.save()
        self.assertEqual(self.admin_post("/admin/llm/models", {}, reauth=True).status_code, 200)
        self.assertEqual(self.seen[-1], {"base_url": "http://saved/v1", "api_key": "stored"})

    def test_no_endpoint_is_a_request_error_and_a_bad_one_is_a_gateway_error(self):
        self.assertEqual(self.admin_post("/admin/llm/models", {}, reauth=True).status_code, 400)
        with patch("clipshelf.interpretation.list_models",
                   side_effect=ConfigurationError("endpoint unreachable")):
            response = self.admin_post(
                "/admin/llm/models", {"base_url": "http://dead/v1"}, reauth=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("unreachable", response.json()["detail"])

    def test_listing_requires_reauthentication(self):
        response = self.admin_post("/admin/llm/models", {"base_url": "http://endpoint/v1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.seen, [])
