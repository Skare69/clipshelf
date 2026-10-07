"""Focused Clipshelf account tests.

Invitation replay, native token login and session revocation, admin boundary
(never content access), and shared-collection transfer limits.
"""

import io
import json
import re
import secrets
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from urllib.error import URLError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.test import Client, SimpleTestCase, TestCase, override_settings
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
from clipshelf.models import Collection, Invitation, Job, Membership, ServerSettings
from clipshelf.services import accessible_collections
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
    *accounts.account_urlpatterns,
    path("api/", include((admin_views.admin_urlpatterns, "clipshelf_accounts_admin"))),
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

    def test_pending_invitation_can_be_renewed_when_link_is_lost(self):
        email = "lost-link@clipshelf.test"
        old_token, old_invitation = make_invitation(email, self.admin)
        self.client.force_login(self.admin)

        response = self.admin_post(
            "/api/admin/invitations", {"email": email}, reauth=True
        )

        self.assertEqual(response.status_code, 201)
        old_invitation.refresh_from_db()
        self.assertLessEqual(old_invitation.expires_at, timezone.now())
        new_token = response.json()["invitation"]["url"].rsplit("/", 1)[-1]
        self.assertNotEqual(new_token, old_token)
        new_invitation = Invitation.objects.get(
            token_hash=invitation_token_hash(new_token)
        )
        self.assertGreater(new_invitation.expires_at, timezone.now())
        self.assertEqual(
            self.client.get(f"/invite/{old_token}").url, reverse("account_login")
        )
        self.assertRedirects(
            self.client.get(f"/invite/{new_token}"),
            f"{reverse('account_signup')}?email=lost-link%40clipshelf.test",
            fetch_redirect_response=False,
        )


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
            f"/api/admin/users/{member.pk}/status", {"active": False}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "reauthentication_required")

        response = self.admin_post(
            f"/api/admin/users/{member.pk}/status", {"active": False}, reauth=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/probe", headers={"x-session-token": token}).status_code,
            401,
        )

        # Re-enabling must not revive the revoked session; the recent
        # reauthentication from above means no password needed now.
        response = self.admin_post(f"/api/admin/users/{member.pk}/status", {"active": True})
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

        response = self.admin_post(f"/api/admin/users/{target.pk}/recover", {})
        self.assertEqual(response.status_code, 403)

        response = self.admin_post(
            f"/api/admin/users/{target.pk}/recover", {}, reauth=True
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
        response = self.admin_post(f"/api/admin/users/{target2.pk}/recover", {})
        self.assertEqual(response.status_code, 200)


class TokenAdminReauthTests(AccountTestCase):
    """Sensitive admin actions over X-Session-Token: request.user must be the
    admin (not anonymous) and reauth bookkeeping must land in the token
    session so the window survives follow-up token requests."""

    def setUp(self):
        super().setUp()
        self.token = self.headless_login(self.admin.email).json()["meta"]["session_token"]
        self.target = make_user("toktarget@clipshelf.test")

    def _post(self, body):
        return self.client.post(
            f"/api/admin/users/{self.target.pk}/status",
            data=json.dumps(body),
            content_type="application/json",
            headers={"x-session-token": self.token},
        )

    def test_reauth_password_disables_and_books_the_window(self):
        with override_settings(ACCOUNT_REAUTHENTICATION_TIMEOUT=0):
            response = self._post({"active": False, "reauth_password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.get(pk=self.target.pk).is_active)
        # A token request never mints a browser session cookie.
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        # The reauth record lives in the token session: no password needed now.
        response = self._post({"active": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(get_user_model().objects.get(pk=self.target.pk).is_active)

    def test_reauth_password_attempts_are_throttled_per_user(self):
        rates = dict(account_settings.RATE_LIMITS)
        rates["reauthenticate"] = "2/m/user"
        # Throwaway cache: the quota starts empty and the attempts spent here
        # can neither inherit from nor leak into other tests.
        with override_settings(
            ACCOUNT_REAUTHENTICATION_TIMEOUT=0,
            ACCOUNT_RATE_LIMITS=rates,
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": f"reauth-throttle-{uuid4().hex}",
                }
            },
        ):
            wrong = {"active": False, "reauth_password": "not-my-password"}
            # Attempt 1 over the cookie session: allowed, wrong password.
            self.client.force_login(self.admin)
            response = self.admin_post(
                f"/api/admin/users/{self.target.pk}/status", wrong
            )
            self.assertEqual(response.status_code, 403)
            # A passwordless challenge between attempts must not spend quota.
            response = self._post({"active": False})
            self.assertEqual(response.status_code, 403)
            # Attempt 2 over the token session: still allowed (2/2), wrong
            # password. Quota is per user, not per session transport.
            response = self._post(wrong)
            self.assertEqual(response.status_code, 403)
            # Attempt 3 carries the correct password but the 2/m/user
            # threshold is spent: JSON 429, the password is never evaluated.
            response = self._post({"active": False, "reauth_password": PASSWORD})
            self.assertEqual(response.status_code, 429)
            self.assertIn("detail", response.json())
            # The limiter blocks before password verification; no action runs.
            self.assertTrue(get_user_model().objects.get(pk=self.target.pk).is_active)
            self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)


class AdminBoundaryTests(AccountTestCase):
    def test_admin_endpoints_reject_anonymous_and_non_admins(self):
        self.assertEqual(self.client.get("/api/admin/users").status_code, 401)
        member = make_user("plain@clipshelf.test")
        client = Client()
        client.force_login(member)
        response = client.get("/api/admin/users")
        self.assertEqual(response.status_code, 403)
        response = client.post(
            f"/api/admin/users/{member.pk}/status", data={"active": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_responses_are_no_store(self):
        # Finding D2: users, invitation links and reset links must never land
        # in private browser caches — success and failure paths alike.
        self.assertEqual(self.client.get("/api/admin/users")["Cache-Control"], "no-store")
        self.client.force_login(self.admin)
        target = make_user("cache-target@clipshelf.test")
        responses = [
            self.admin_post(f"/api/admin/users/{target.pk}/recover", {}),
            self.client.get("/api/admin/users"),
            self.admin_post(
                "/api/admin/invitations",
                {"email": "invited@clipshelf.test"},
                reauth=True,
            ),
            self.admin_post(f"/api/admin/users/{target.pk}/recover", {}, reauth=True),
            self.admin_post("/api/admin/invitations", {"email": "not-an-email"}),
        ]
        self.assertEqual(
            [r.status_code for r in responses], [403, 200, 201, 200, 400]
        )
        for response in responses:
            self.assertEqual(response["Cache-Control"], "no-store")

    def test_admin_role_grants_no_content_access(self):
        other = make_user("other@clipshelf.test")
        shared = Collection(name="Family", kind="shared", owner=other)
        shared.save()
        Membership(collection=shared, user=other).save()
        self.client.force_login(self.admin)
        response = self.client.get("/api/admin/collections")
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
        url = f"/api/admin/collections/{shared.pk}/transfer"

        self.client.force_login(self.admin)
        # Personal collections never transfer.
        self.assertEqual(
            self.admin_post(
                f"/api/admin/collections/{personal.pk}/transfer",
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

        self.admin_post(f"/api/admin/users/{owner.pk}/status", {"active": False})

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
        self.admin_post(f"/api/admin/users/{owner.pk}/status", {"active": True})
        shared.refresh_from_db()
        self.assertEqual(shared.owner, member)

    def test_transfer_keeps_old_owner_and_clears_new_owner_membership(self):
        owner = make_user("former-owner@clipshelf.test", is_active=False)
        member = make_user("new-owner@clipshelf.test")
        shared = Collection(name="House", kind="shared", owner=owner)
        shared.save()
        Membership(collection=shared, user=member).save()
        self.client.force_login(self.admin)

        response = self.admin_post(
            f"/api/admin/collections/{shared.pk}/transfer",
            {"owner_id": str(member.pk)},
            reauth=True,
        )

        self.assertEqual(response.status_code, 200)
        owner.is_active = True
        owner.save(update_fields=["is_active"])
        self.assertTrue(
            Membership.objects.filter(collection=shared, user=owner).exists()
        )
        self.assertFalse(
            Membership.objects.filter(collection=shared, user=member).exists()
        )
        self.assertTrue(
            accessible_collections(owner).filter(pk=shared.pk).exists()
        )
        self.assertTrue(
            accessible_collections(member).filter(pk=shared.pk).exists()
        )

    def test_method_not_allowed_is_json_on_the_production_mount(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/api/admin/users", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json(), {"detail": "method_not_allowed"})


class LlmModelPickerTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.requests = []

        def respond(request, **kwargs):
            self.requests.append((request.full_url, request.get_header("Authorization")))
            return io.BytesIO(b'{"data":[{"id":"vision-a"}]}')

        patcher = patch("urllib.request.urlopen", side_effect=respond)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keys_stay_with_their_endpoint_and_explicit_clear_wins(self):
        saved, other = "http://saved/v1", "http://other/v1"
        self.admin_post("/api/admin/llm", {"base_url": saved, "api_key": "stored"}, reauth=True)
        # A trailing slash is the same endpoint: the saved key still applies.
        response = self.admin_post("/api/admin/llm/models", {"base_url": saved + "/"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[-1][1], "Bearer stored")
        for body, expected in (
            ({}, (saved + "/models", "Bearer stored")),
            ({"base_url": other}, (other + "/models", None)),
            ({"base_url": other, "api_key": "typed"}, (other + "/models", "Bearer typed")),
            ({"api_key": ""}, (saved + "/models", None)),
        ):
            response = self.admin_post("/api/admin/llm/models", body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["models"], ["vision-a"])
            self.assertEqual(self.requests[-1], expected)
            self.assertNotIn("stored", response.content.decode())

        response = self.admin_post("/api/admin/llm", {"base_url": other, "model": "m"})
        self.assertFalse(response.json()["llm"]["has_api_key"])
        self.admin_post("/api/admin/llm/models", {})
        self.assertEqual(self.requests[-1], (other + "/models", None))

        self.admin_post("/api/admin/llm", {"api_key": "replacement"})
        response = self.admin_post("/api/admin/llm", {"base_url": other, "model": "new"})
        self.assertTrue(response.json()["llm"]["has_api_key"])
        self.admin_post("/api/admin/llm/models", {})
        self.assertEqual(self.requests[-1], (other + "/models", "Bearer replacement"))
        response = self.admin_post("/api/admin/llm", {"api_key": ""})
        self.assertFalse(response.json()["llm"]["has_api_key"])
        self.admin_post("/api/admin/llm/models", {})
        self.assertEqual(self.requests[-1], (other + "/models", None))

    def test_previews_never_persist_and_never_invalidate_verification(self):
        self.admin_post("/api/admin/llm", {"base_url": "http://saved/v1", "model": "m",
                                       "api_key": "stored"}, reauth=True)
        s = ServerSettings.objects.get(pk=1)
        s.llm_verified_at = timezone.now()
        s.save()
        self.admin_post("/api/admin/llm/check",
                        {"base_url": "http://typed/v1", "model": "x", "api_key": "typed"})
        self.admin_post("/api/admin/llm/models", {"base_url": "http://typed/v1", "api_key": "typed"})
        s.refresh_from_db()
        self.assertEqual(s.llm_base_url, "http://saved/v1")
        self.assertEqual(s.llm_model, "m")
        self.assertEqual(s.llm_api_key, "stored")
        self.assertIsNotNone(s.llm_verified_at)
        self.admin_post("/api/admin/llm/models", {})
        self.assertEqual(self.requests[-1], ("http://saved/v1/models", "Bearer stored"))

    def test_verification_resets_only_on_actual_capability_changes(self):
        self.admin_post("/api/admin/llm", {"base_url": "http://saved/v1", "model": "m",
                                       "api_key": "stored"}, reauth=True)
        s = ServerSettings.objects.get(pk=1)
        s.llm_verified_at = timezone.now()
        s.save()
        # Re-posting identical values (trailing slash aside) keeps verification.
        self.admin_post("/api/admin/llm", {"base_url": "http://saved/v1/", "model": "m",
                                       "api_key": "stored"}, reauth=True)
        s.refresh_from_db()
        self.assertIsNotNone(s.llm_verified_at)
        # Concurrency-only changes never invalidate.
        self.admin_post("/api/admin/llm", {"concurrency": 6}, reauth=True)
        s.refresh_from_db()
        self.assertEqual(s.llm_concurrency, 6)
        self.assertIsNotNone(s.llm_verified_at)
        # A model change does.
        self.admin_post("/api/admin/llm", {"model": "new"}, reauth=True)
        s.refresh_from_db()
        self.assertIsNone(s.llm_verified_at)

    def test_direct_model_save_never_carries_a_key_to_a_new_host(self):
        self.admin_post("/api/admin/llm", {"base_url": "http://saved/v1", "api_key": "stored"},
                        reauth=True)
        s = ServerSettings.objects.get(pk=1)
        s.llm_base_url = "http://moved/v1"
        s.save(update_fields=["llm_base_url"])
        s.refresh_from_db()
        self.assertEqual(s.llm_api_key, "")
        self.assertIsNone(s.llm_verified_at)

    def test_retyped_key_survives_an_endpoint_move(self):
        """The admin re-entered the key for the new endpoint (the check preview
        validated exactly those values): the save must keep it. A key merely
        carried over from the old endpoint is still isolated."""
        self.admin_post("/api/admin/llm", {"base_url": "http://saved/v1", "api_key": "stored"},
                        reauth=True)
        response = self.admin_post(
            "/api/admin/llm", {"base_url": "http://moved/v1", "api_key": "stored"})
        self.assertTrue(response.json()["llm"]["has_api_key"])
        self.admin_post("/api/admin/llm/models", {})
        self.assertEqual(self.requests[-1], ("http://moved/v1/models", "Bearer stored"))
        s = ServerSettings.objects.get(pk=1)
        self.assertEqual(s.llm_api_key, "stored")
        self.assertIsNone(s.llm_verified_at)  # capability check reruns on the new endpoint
        response = self.admin_post("/api/admin/llm", {"base_url": "http://third/v1"})
        self.assertFalse(response.json()["llm"]["has_api_key"])

    def test_check_probes_the_typed_values_not_stale_saved_ones(self):
        saved = "http://saved/v1"
        self.admin_post("/api/admin/llm", {"base_url": saved, "model": "old",
                                       "api_key": "stored"}, reauth=True)
        for body, expected in (
            ({}, (saved + "/chat/completions", "Bearer stored")),
            ({"base_url": "http://typed/v1", "model": "new"},
             ("http://typed/v1/chat/completions", None)),
            ({"api_key": "typed"}, (saved + "/chat/completions", "Bearer typed")),
            ({"api_key": ""}, (saved + "/chat/completions", None)),
        ):
            response = self.admin_post("/api/admin/llm/check", body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.requests[-1], expected)

    def test_check_with_no_config_reports_inline_without_a_request(self):
        response = self.admin_post("/api/admin/llm/check", {}, reauth=True)
        self.assertEqual(response.status_code, 200)
        check = response.json()["check"]
        self.assertFalse(check["ok"])
        self.assertIn("http(s)", check["message"])
        self.assertEqual(self.requests, [])

    def test_no_endpoint_is_a_request_error_and_an_unreachable_one_is_a_gateway_error(self):
        self.assertEqual(self.admin_post("/api/admin/llm/models", {}, reauth=True).status_code, 400)
        with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
            response = self.admin_post("/api/admin/llm/models", {"base_url": "http://dead/v1"})
        self.assertEqual(response.status_code, 502)

    def test_listing_requires_admin_reauthentication_before_any_request(self):
        body = {"base_url": "http://endpoint/v1"}
        self.assertEqual(self.admin_post("/api/admin/llm/models", body).status_code, 403)
        self.client.logout()
        self.assertEqual(self.admin_post("/api/admin/llm/models", body).status_code, 401)
        self.client.force_login(make_user("member@clipshelf.test"))
        self.assertEqual(self.admin_post("/api/admin/llm/models", body, reauth=True).status_code, 403)
        self.assertEqual(self.requests, [])


class InvitationUrlOriginTests(AccountTestCase):
    """The copyable invitation link follows the approved canonical origin;
    a spoofed Host header must never choose its destination (finding D3)."""

    def _admin_post_invitation(self, **client_kwargs):
        return self.client.post(
            "/api/admin/invitations",
            data=json.dumps(
                {"email": "newbie@clipshelf.test", "reauth_password": PASSWORD}
            ),
            content_type="application/json",
            **client_kwargs,
        )

    def test_configured_origin_overrides_host_header(self):
        self.client.force_login(self.admin)
        with override_settings(CLIPSHELF_ORIGIN="https://clipshelf.example"):
            response = self._admin_post_invitation(HTTP_HOST="evil.example")
        self.assertEqual(response.status_code, 201)
        url = response.json()["invitation"]["url"]
        self.assertTrue(url.startswith("https://clipshelf.example/invite/"))

    def test_unset_origin_keeps_the_request_host_fallback(self):
        self.client.force_login(self.admin)
        response = self._admin_post_invitation()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            response.json()["invitation"]["url"].startswith(
                "http://testserver/invite/"
            )
        )


class AccountMailOriginTests(AccountTestCase):
    """Account mail never carries a link whose host came from the request
    Host header: configured origin, or a path-only link until one is set."""

    def setUp(self):
        super().setUp()
        self.evil = Client(HTTP_HOST="evil.example")

    def _mail_links(self):
        bodies = "\n".join(message.body for message in mail.outbox)
        self.assertNotIn("evil.example", bodies)
        return re.findall(r"\S*/accounts/\S+", bodies)

    def test_reset_and_unknown_account_mail_ignore_host_header(self):
        make_user("victim@clipshelf.test")
        for email in ("victim@clipshelf.test", "nobody@clipshelf.test"):
            self.evil.post("/accounts/password/reset/", {"email": email})
        links = self._mail_links()
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0].startswith("/accounts/password/reset/key/"), links)
        self.assertEqual(links[1], "/accounts/signup/")

    def test_confirmation_mail_follows_configured_origin(self):
        # Own mailbox: allauth's confirm_email rate limit lives in the shared cache.
        token, _ = make_invitation("mailorigin@clipshelf.test", self.admin)
        self.evil.get(f"/invite/{token}")
        with override_settings(CLIPSHELF_ORIGIN="https://clipshelf.example"):
            self.evil.post("/accounts/signup/", {
                "email": "mailorigin@clipshelf.test",
                "password1": PASSWORD,
                "password2": PASSWORD,
            })
        links = self._mail_links()
        self.assertEqual(len(links), 1)
        self.assertTrue(
            links[0].startswith("https://clipshelf.example/accounts/confirm-email/"),
            links,
        )

    def test_check_warns_when_smtp_has_no_origin(self):
        from clipshelf.project.apps import mail_origin_check

        smtp = "django.core.mail.backends.smtp.EmailBackend"
        with override_settings(EMAIL_BACKEND=smtp, CLIPSHELF_ORIGIN=""):
            self.assertEqual([w.id for w in mail_origin_check(None)], ["clipshelf.W001"])
        with override_settings(EMAIL_BACKEND=smtp, CLIPSHELF_ORIGIN="http://nas:8000"):
            self.assertEqual(mail_origin_check(None), [])



@override_settings(ROOT_URLCONF="clipshelf.urls", TEMPLATES=TEMPLATES)
class ProductionAdminMountTests(TestCase):
    def test_production_mount_serves_every_admin_route(self):
        admin = make_user("mount-admin@clipshelf.test", is_app_admin=True)
        names = [route.name for route in admin_views.admin_urlpatterns]
        self.assertEqual(len(names), len(set(names)))
        urls = []
        for route in admin_views.admin_urlpatterns:
            kwargs = {
                kwarg: str(uuid4())
                for kwarg in re.findall(r"<[^:>]+:(\w+)>", str(route.pattern))
            }
            url = reverse(f"clipshelf_accounts_admin:{route.name}", kwargs=kwargs)
            self.assertTrue(url.startswith("/api/admin/"), url)
            urls.append(url)
        self.assertIn("/api/admin/screening", urls)
        self.assertIn("/api/admin/screening/check", urls)
        client = Client()
        for url in urls:
            response = client.get(url)
            self.assertIn(response.status_code, (401, 405), url)
            self.assertEqual(response["Cache-Control"], "no-store")
        client.force_login(admin)
        response = client.get("/api/admin/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn(admin.email, json.dumps(response.json()))


class JobPolicyTests(SimpleTestCase):
    """Read-only Job predicates (contract A) consumed by the HTTP layer."""

    @staticmethod
    def _job(**overrides):
        fields = {
            "state": Job.State.QUEUED,
            "acquisition": Job.AcquisitionStatus.PENDING,
            "interpretation": Job.InterpretationStatus.PENDING,
        }
        fields.update(overrides)
        return Job(**fields)

    def test_active_jobs_are_never_retryable_or_attention_seeking(self):
        for state in (Job.State.QUEUED, Job.State.RUNNING, Job.State.RETRY):
            job = self._job(state=state)
            self.assertTrue(job.active)
            self.assertFalse(job.can_retry)
            self.assertFalse(job.needs_attention)

    def test_finished_jobs_stay_reprocessable(self):
        for state in (Job.State.BLOCKED, Job.State.DONE):
            job = self._job(state=state)
            self.assertFalse(job.active)
            self.assertTrue(job.can_retry)

    def test_attention_covers_blocked_state_and_stage_failures(self):
        self.assertTrue(self._job(state=Job.State.BLOCKED).needs_attention)
        for field, status in (
            ("acquisition", Job.AcquisitionStatus.BLOCKED),
            ("acquisition", Job.AcquisitionStatus.ERROR),
            ("interpretation", Job.InterpretationStatus.BLOCKED),
            ("interpretation", Job.InterpretationStatus.ERROR),
        ):
            self.assertTrue(self._job(**{field: status}).needs_attention)
        self.assertFalse(
            self._job(
                state=Job.State.DONE,
                acquisition=Job.AcquisitionStatus.COMPLETE,
                interpretation=Job.InterpretationStatus.COMPLETE,
            ).needs_attention
        )
