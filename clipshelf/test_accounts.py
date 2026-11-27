"""Focused Clipshelf account tests.

Invitation replay, native token login and session revocation, admin boundary
(never content access), and shared-collection transfer limits.
"""

import json
import random
import re
import secrets
import time
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from allauth.account import app_settings as account_settings
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str

from clipshelf import accounts, admin_views
from clipshelf.accounts import (
    SESSION_INVITATION_KEY,
    api_auth,
    invitation_token_hash,
)
from clipshelf.api import json_error
from clipshelf.models import (
    Collection,
    FollowedSource,
    Invitation,
    Job,
    Membership,
    ServerSettings,
)
from clipshelf.interpretation import _TransportError
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
        user, _ = api_auth(request)
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


@override_settings(ROOT_URLCONF="clipshelf.test_accounts", TEMPLATES=TEMPLATES)
class BrowserLoginCsrfTests(TestCase):
    """The allauth browser login checks the CSRF token even with
    CLIPSHELF_CSRF off: SameSite=Lax cannot stop a cross-site login POST,
    because the request needs no existing cookie."""

    def test_login_without_token_is_rejected_and_with_token_logs_in(self):
        make_user("victim@clipshelf.test")
        client = Client(enforce_csrf_checks=True)
        evil = client.post(
            "/accounts/login/",
            data={"login": "victim@clipshelf.test", "password": PASSWORD},
            HTTP_ORIGIN="https://evil.example",
        )
        self.assertEqual(evil.status_code, 403)
        self.assertNotIn("_auth_user_id", client.session)

        client.get("/accounts/login/")
        good = client.post(
            "/accounts/login/",
            data={
                "login": "victim@clipshelf.test",
                "password": PASSWORD,
                "csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value,
            },
            HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(good.status_code, 302)
        self.assertIn("_auth_user_id", client.session)

    def test_headless_login_stays_exempt_from_browser_csrf(self):
        # The Android client authenticates through /_allauth/ with the
        # session-token strategy and must keep working without a CSRF token.
        make_user("mobile@clipshelf.test")
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/_allauth/app/v1/auth/login",
            data=json.dumps(
                {"email": "mobile@clipshelf.test", "password": PASSWORD}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


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

    def test_disabled_issuer_invitation_cannot_create_account(self):
        issuer = make_user("issuer@clipshelf.test", is_app_admin=True)
        token, invitation = make_invitation("late@clipshelf.test", issuer)
        # A second session already holds the link before the disable.
        early = Client()
        early.get(f"/invite/{token}")
        self.assertIn(SESSION_INVITATION_KEY, early.session)

        self.client.force_login(self.admin)
        response = self.admin_post(
            f"/api/admin/users/{issuer.pk}/status", {"active": False}, reauth=True
        )
        self.assertEqual(response.status_code, 200)

        response = Client().get(f"/invite/{token}")
        self.assertRedirects(
            response, reverse("account_login"), fetch_redirect_response=False
        )
        signup = {
            "email": "late@clipshelf.test",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        response = early.post(reverse("account_signup"), data=signup)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            get_user_model().objects.filter(email="late@clipshelf.test").exists()
        )
        invitation.refresh_from_db()
        self.assertIsNone(invitation.used_by)

        # Re-enabling the issuer does not revive the expired link.
        self.admin_post(f"/api/admin/users/{issuer.pk}/status", {"active": True})
        response = Client().get(f"/invite/{token}")
        self.assertRedirects(
            response, reverse("account_login"), fetch_redirect_response=False
        )

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

    def test_mixed_case_registered_email_blocks_invitation(self):
        # Mailbox comparison is case-insensitive everywhere else (invite
        # redemption, _email_held, create_admin). Rows written by a path
        # that bypasses User.save() keep their original case — the CI
        # unique constraint allows a lone mixed-case row — so the
        # duplicate check must not compare exactly.
        squatter = make_user("Admin@X.com")
        get_user_model().objects.filter(pk=squatter.pk).update(
            email="Admin@X.com"
        )
        EmailAddress.objects.filter(user=squatter).update(email="Admin@X.com")
        self.client.force_login(self.admin)

        response = self.admin_post(
            "/api/admin/invitations", {"email": "admin@x.com"}, reauth=True
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"detail": "That email is already registered."}
        )
        self.assertEqual(Invitation.objects.count(), 0)

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

    def test_unverified_secondary_email_does_not_block_invitation_signup(self):
        for active in (True, False):
            with self.subTest(active=active):
                state = "active" if active else "inactive"
                email = f"invitee-{state}@clipshelf.test"
                squatter = make_user(f"squatter-{state}@clipshelf.test")
                EmailAddress.objects.create(
                    user=squatter, email=f"unrelated-{state}@clipshelf.test"
                )
                retained = list(
                    EmailAddress.objects.filter(user=squatter).values_list(
                        "pk", "email", "primary", "verified"
                    )
                )
                squatter_client = Client()
                squatter_client.force_login(squatter)
                # Reauthentication resumes the parked email-add request.
                response = squatter_client.post(
                    reverse("account_email"),
                    data={"action_add": "", "email": email},
                )
                self.assertEqual(response.status_code, 302)
                response = squatter_client.post(
                    response.url, data={"password": PASSWORD}
                )
                self.assertEqual(response.status_code, 302)
                claim = EmailAddress.objects.get(user=squatter, email=email)
                self.assertFalse(claim.verified)
                self.assertFalse(claim.primary)
                squatter.is_active = active
                squatter.save(update_fields=["is_active"])
                token, invitation = make_invitation(email, self.admin)
                invitation.email = email.swapcase()
                invitation.save(update_fields=["email"])
                client = Client()
                client.get(f"/invite/{token}")
                mail.outbox.clear()

                response = client.post(
                    reverse("account_signup"),
                    data={
                        "email": email.title(),
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    },
                )

                self.assertEqual(response.status_code, 302)
                invitee = get_user_model().objects.get(email=email)
                invitation.refresh_from_db()
                self.assertEqual(invitation.used_by, invitee)
                self.assertIsNotNone(invitation.used_at)
                self.assertNotIn(SESSION_INVITATION_KEY, client.session)
                self.assertEqual(
                    list(
                        EmailAddress.objects.filter(email__iexact=email).values_list(
                            "user_id", "verified", "primary"
                        )
                    ),
                    [(invitee.pk, False, True)],
                )
                self.assertEqual(
                    list(
                        EmailAddress.objects.filter(user=squatter).values_list(
                            "pk", "email", "primary", "verified"
                        )
                    ),
                    retained,
                )
                client.force_login(invitee)
                self.assertEqual(client.get("/probe").status_code, 401)
                before = len(mail.outbox)
                # The earlier email-add send consumes the 180-second mailbox limit.
                after_cooldown = time.time() + 181
                login_client = Client()
                with patch(
                    "allauth.core.internal.ratelimit.time", wraps=time
                ) as clock:
                    clock.time.return_value = after_cooldown
                    response = login_client.post(
                        reverse("account_login"),
                        data={"login": email, "password": PASSWORD},
                    )
                self.assertRedirects(
                    response,
                    reverse("account_email_verification_sent"),
                    fetch_redirect_response=False,
                )
                self.assertNotIn("_auth_user_id", login_client.session)
                self.assertTrue(
                    any(
                        message.to == [email] and "confirm" in message.subject.lower()
                        for message in mail.outbox[before:]
                    )
                )
                self.assertFalse(
                    any(squatter.email in message.to for message in mail.outbox)
                )

    def test_protected_email_claims_still_block_invitation_signup(self):
        for protection in ("verified", "primary", "user_email"):
            with self.subTest(protection=protection):
                email = f"protected-{protection}@clipshelf.test"
                holder = make_user(f"holder-{protection}@clipshelf.test")
                if protection == "primary":
                    EmailAddress.objects.filter(user=holder).update(primary=False)
                if protection == "user_email":
                    holder.email = email
                    holder.save(update_fields=["email"])
                claim = EmailAddress.objects.create(
                    user=holder,
                    email=email,
                    primary=protection == "primary",
                    verified=protection == "verified",
                )
                token, invitation = make_invitation(email, self.admin)
                client = Client()
                client.get(f"/invite/{token}")
                before = get_user_model().objects.count()

                client.post(
                    reverse("account_signup"),
                    data={
                        "email": email.swapcase(),
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    },
                )

                self.assertEqual(get_user_model().objects.count(), before)
                invitation.refresh_from_db()
                self.assertIsNone(invitation.used_by)
                self.assertIsNone(invitation.used_at)
                self.assertTrue(EmailAddress.objects.filter(pk=claim.pk).exists())

    def test_failed_invitation_consumption_preserves_secondary_claim(self):
        holder = make_user("stale-holder@clipshelf.test")
        for failure in ("expired", "used", "invalid_token"):
            with self.subTest(failure=failure):
                email = f"stale-{failure}@clipshelf.test"
                claim = EmailAddress.objects.create(user=holder, email=email)
                token, invitation = make_invitation(email, self.admin)
                request = RequestFactory().post(reverse("account_signup"))
                request.user = AnonymousUser()
                request.session = {SESSION_INVITATION_KEY: token}
                form = accounts.InviteSignupForm(
                    data={
                        "email": email,
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    }
                )
                with accounts.allauth_context.request_context(request):
                    self.assertTrue(form.is_valid(), form.errors)
                    self.assertFalse(form.account_already_exists)
                    # Invalidate the invitation after validation but before save.
                    if failure == "expired":
                        invitation.expires_at = timezone.now() - timedelta(seconds=1)
                        invitation.save(update_fields=["expires_at"])
                    elif failure == "used":
                        invitation.used_by = self.admin
                        invitation.used_at = timezone.now()
                        invitation.save(update_fields=["used_by", "used_at"])
                    else:
                        request.session[SESSION_INVITATION_KEY] = "invalid-token"
                    before = get_user_model().objects.count()
                    mail.outbox.clear()
                    with self.assertRaises(ValidationError):
                        form.save(request)

                self.assertEqual(get_user_model().objects.count(), before)
                self.assertFalse(get_user_model().objects.filter(email=email).exists())
                claim.refresh_from_db()
                self.assertEqual(claim.user, holder)
                self.assertFalse(claim.verified)
                self.assertFalse(claim.primary)
                self.assertEqual(mail.outbox, [])

    def test_confirmed_competing_claim_between_validation_and_save_rolls_back_signup(self):
        holder = make_user("race-holder@clipshelf.test")
        email = "race-invitee@clipshelf.test"
        claim = EmailAddress.objects.create(user=holder, email=email)
        token, invitation = make_invitation(email, self.admin)
        request = RequestFactory().post(reverse("account_signup"))
        request.user = AnonymousUser()
        request.session = {SESSION_INVITATION_KEY: token}
        form = accounts.InviteSignupForm(
            data={
                "email": email,
                "password1": PASSWORD,
                "password2": PASSWORD,
            }
        )
        with accounts.allauth_context.request_context(request):
            self.assertTrue(form.is_valid(), form.errors)
            self.assertFalse(form.account_already_exists)
            # The holder confirms the mailbox between validation and save of
            # the invitee's request.
            claim.verified = True
            claim.save(update_fields=["verified"])
            before = get_user_model().objects.count()
            mail.outbox.clear()
            with self.assertRaises(ValidationError):
                form.save(request)

        self.assertEqual(get_user_model().objects.count(), before)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.used_by)
        self.assertIsNone(invitation.used_at)
        claim.refresh_from_db()
        self.assertEqual(claim.user, holder)
        self.assertTrue(claim.verified)
        self.assertFalse(claim.primary)
        self.assertEqual(mail.outbox, [])

    def test_invited_email_uniqueness_matches_seeded_policy(self):
        seed = 137
        rng = random.Random(seed)
        for case in range(128):
            email = f"policy-{case}@clipshelf.test"
            claim_count = rng.randrange(4)
            # Each mailbox permits one verified claim and one User.email holder.
            verified_holder = rng.randrange(-1, claim_count)
            user_email_holder = rng.randrange(-1, claim_count)
            claims = [
                (
                    index == verified_holder,
                    rng.choice((False, True)),
                    index == user_email_holder,
                    rng.choice((False, True)),
                )
                for index in range(claim_count)
            ]
            with self.subTest(seed=seed, case=case, claims=claims):
                for index, (verified, primary, user_email, active) in enumerate(claims):
                    holder = get_user_model().objects.create(
                        username=f"policy-{case}-{index}",
                        email=(
                            email if user_email
                            else f"holder-{case}-{index}@clipshelf.test"
                        ),
                        is_active=active,
                    )
                    EmailAddress.objects.create(
                        user=holder, email=email, verified=verified, primary=primary,
                    )
                token, invitation = make_invitation(email, self.admin)
                invitation.email = email.swapcase()
                invitation.save(update_fields=["email"])
                request = RequestFactory().get(reverse("account_signup"))
                request.user = AnonymousUser()
                request.session = {SESSION_INVITATION_KEY: token}
                form = accounts.InviteSignupForm(
                    data={
                        "email": email.title(),
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                    }
                )
                with accounts.allauth_context.request_context(request):
                    self.assertTrue(form.is_valid(), form.errors)
                blocked = any(
                    verified or primary or user_email
                    for verified, primary, user_email, _ in claims
                )
                self.assertEqual(form.account_already_exists, blocked)
                self.assertEqual(form.cleaned_data["email"], email)


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


_CSRF_MIDDLEWARE = list(settings.MIDDLEWARE)
_CSRF_MIDDLEWARE.insert(
    _CSRF_MIDDLEWARE.index("django.middleware.common.CommonMiddleware") + 1,
    "django.middleware.csrf.CsrfViewMiddleware",
)


@override_settings(
    ROOT_URLCONF="clipshelf.test_accounts",
    TEMPLATES=TEMPLATES,
    CLIPSHELF_CSRF=True,
    MIDDLEWARE=_CSRF_MIDDLEWARE,
)
class AdminCsrfExemptTests(AccountTestCase):
    """With the global CsrfViewMiddleware installed, the admin URL callbacks
    must carry csrf_exempt like the /api wrapper: the middleware never answers
    before api_guard (401 JSON for anonymous, 200 JSON for token clients)."""

    def test_anonymous_post_answers_401_json(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/api/admin/invitations", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "unauthenticated"})

    def test_token_post_with_reauth_answers_200_json(self):
        token = self.headless_login(self.admin.email).json()["meta"]["session_token"]
        target = make_user("csrftarget@clipshelf.test")
        client = Client(enforce_csrf_checks=True)
        with override_settings(ACCOUNT_REAUTHENTICATION_TIMEOUT=0):
            response = client.post(
                f"/api/admin/users/{target.pk}/status",
                data=json.dumps({"active": False, "reauth_password": PASSWORD}),
                content_type="application/json",
                headers={"x-session-token": token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.get(pk=target.pk).is_active)

    def test_every_admin_callback_is_csrf_exempt(self):
        for pattern in admin_views.admin_urlpatterns:
            self.assertTrue(
                getattr(pattern.callback, "csrf_exempt", False),
                f"{pattern.pattern} is not csrf_exempt",
            )


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

    def test_oversized_admin_body_answers_json_413(self):
        # The shared /api body cap: an admin body over 64 KiB is rejected
        # before any view logic, with a JSON answer and no caching.
        self.client.force_login(self.admin)
        response = self.admin_post(
            "/api/admin/invitations", {"email": "x" * (64 * 1024)}
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "request body too large"})
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

        def fake_request(method, url, key, payload, timeout):
            self.requests.append((url, f"Bearer {key}" if key else None))
            return {"data": [{"id": "vision-a"}]}

        patcher = patch("clipshelf.interpretation._request", side_effect=fake_request)
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

    def test_output_tokens_setting(self):
        r = self.client.get("/api/admin/llm")
        self.assertEqual(r.json()["llm"]["output_tokens"], 4000)
        s = ServerSettings.objects.get(pk=1)
        s.llm_verified_at = timezone.now()
        s.save()
        r = self.admin_post("/api/admin/llm", {"output_tokens": 6000}, reauth=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["llm"]["output_tokens"], 6000)
        s.refresh_from_db()
        self.assertEqual(s.llm_output_tokens, 6000)
        self.assertIsNotNone(s.llm_verified_at)
        for bad in (499, 8001, "x", None):
            r = self.admin_post("/api/admin/llm", {"output_tokens": bad}, reauth=True)
            self.assertEqual(r.status_code, 400, bad)
            s.refresh_from_db()
            self.assertEqual(s.llm_output_tokens, 6000)

    def test_check_probe_sends_typed_output_tokens(self):
        """The connection probe derives its limit from the typed output tokens
        on screen; omission or a non-integer falls back to the saved setting."""
        s = admin_views.get_settings()
        s.llm_base_url, s.llm_model, s.llm_output_tokens = "http://saved/v1", "m", 6000
        s.save()
        sent = []
        patcher = patch("clipshelf.interpretation._request",
                        side_effect=lambda m, u, k, p, t: sent.append(p) or {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin_post("/api/admin/llm/check", {"output_tokens": 5500}, reauth=True)
        self.assertEqual(sent[-1]["max_tokens"], 5500)
        self.admin_post("/api/admin/llm/check", {}, reauth=True)
        self.assertEqual(sent[-1]["max_tokens"], 6000)
        self.admin_post("/api/admin/llm/check", {"output_tokens": "5500"}, reauth=True)
        self.assertEqual(sent[-1]["max_tokens"], 6000)

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
        with patch("clipshelf.interpretation._request",
                   side_effect=_TransportError("unreachable", detail="connection refused")):
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


class MalformedAdminBodyTests(AccountTestCase):
    def test_deep_json_on_admin_endpoint_is_json_400(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/api/admin/invitations",
            data=b"[" * 60000,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertIn("detail", response.json())


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

    def test_over_memory_limit_admin_body_answers_json_413(self):
        # A body past DATA_UPLOAD_MAX_MEMORY_SIZE raises RequestDataTooBig;
        # the shared /api wrapper core maps it to a JSON 413 (never an HTML
        # framework page), like every other oversized-body answer.
        admin = make_user("mount-admin@clipshelf.test", is_app_admin=True)
        client = Client()
        client.force_login(admin)
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024):
            response = client.post(
                "/api/admin/invitations",
                data=b"x" * 2048,
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 413)
        self.assertTrue(response["Content-Type"].startswith("application/json"))
        self.assertEqual(response.json()["detail"], "request body too large")
        self.assertEqual(response["Cache-Control"], "no-store")


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
            # A failed stage under scheduler retry (models.Job.fail) is not
            # the operator's problem yet: can_retry is false and the job line
            # already says "Retrying soon".
            self.assertFalse(
                self._job(
                    state=state, acquisition=Job.AcquisitionStatus.ERROR
                ).needs_attention
            )

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
            # error set: a bare done+interpretation-blocked row is a shelved
            # terminal link (see the terminal test), not a failure.
            self.assertTrue(
                self._job(
                    state=Job.State.DONE, error="stage failed", **{field: status}
                ).needs_attention
            )
        self.assertFalse(
            self._job(
                state=Job.State.DONE,
                acquisition=Job.AcquisitionStatus.COMPLETE,
                interpretation=Job.InterpretationStatus.COMPLETE,
            ).needs_attention
        )

    def test_terminal_host_job_is_shelved_not_attention(self):
        job = self._job(
            state=Job.State.DONE,
            acquisition=Job.AcquisitionStatus.COMPLETE,
            interpretation=Job.InterpretationStatus.BLOCKED,
        )
        self.assertTrue(job.terminal)
        self.assertFalse(job.needs_attention)
        self.assertTrue(job.can_retry)
        # the same trio with an error is a real failure again
        job = self._job(
            state=Job.State.DONE,
            acquisition=Job.AcquisitionStatus.COMPLETE,
            interpretation=Job.InterpretationStatus.BLOCKED,
            error="upstream refused",
        )
        self.assertFalse(job.terminal)
        self.assertTrue(job.needs_attention)


class InviteSignupFormGuardTests(AccountTestCase):
    """The signup form turns an unknown mailbox into a form error, driven by
    allauth's request context even though allauth hides the request from
    signup-form clean_email."""

    def test_clean_email_rejects_unknown_email_without_pending_invitation(self):
        request = RequestFactory().get("/signup")
        request.user = AnonymousUser()
        request.session = {}
        # allauth 65.x hides the request from signup forms: it reaches
        # clean_email only through allauth's request context.
        form = accounts.InviteSignupForm(data={"email": "stranger@clipshelf.test"})
        # Production shape: fields are cleaned before clean_email runs.
        form.cleaned_data = {"email": "stranger@clipshelf.test"}
        with accounts.allauth_context.request_context(request):
            with self.assertRaises(ValidationError):
                form.clean_email()


@override_settings(ROOT_URLCONF="clipshelf.urls", TEMPLATES=TEMPLATES)
class MemberRemovalSecondaryAddressTests(TestCase):
    """Removal must burn invitations to every address of the removed account,
    not only to the address that is primary at removal time."""

    def setUp(self):
        inline = patch.object(accounts, "deliver_async", lambda fn: fn())
        inline.start()
        self.addCleanup(inline.stop)
        cache.clear()  # the owner-keyed invite throttle lives in the cache
        self.owner = make_user("owner@clipshelf.test")
        self.member = make_user("member-b@clipshelf.test")
        EmailAddress.objects.create(
            user=self.member, email="member-a@clipshelf.test", verified=True
        )
        self.shared = Collection.objects.create(
            name="Team finds", kind="shared", owner=self.owner
        )
        self.owner_client = Client()
        self.owner_client.force_login(self.owner)
        self.member_client = Client()
        self.member_client.force_login(self.member)

    def _invite(self, email):
        response = self.owner_client.post(
            f"/api/collections/{self.shared.id}/members",
            data=json.dumps({"email": email}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)

    def _make_primary(self, email):
        response = self.member_client.post(
            reverse("account_email"), data={"action_primary": "", "email": email}
        )
        if "reauthenticate" in response.get("Location", ""):
            response = self.member_client.post(
                response["Location"], data={"password": PASSWORD}
            )
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, email)

    def _link(self, email):
        """Path of the newest invitation link mailed to this address."""
        return [
            urlsplit(line.strip()).path
            for message in mail.outbox
            if message.to == [email]
            for line in message.body.splitlines()
            if "/collections/invitations/" in line
        ][-1]

    def _is_member(self):
        return Membership.objects.filter(
            collection=self.shared, user=self.member
        ).exists()

    def test_invitation_to_secondary_address_mails_nothing(self):
        self._invite("member-a@clipshelf.test")
        self.assertEqual(mail.outbox, [])

    def test_removal_burns_link_mailed_to_a_former_primary_address(self):
        # Address B is primary: the owner's invitation mails a live link to B.
        self._invite("member-b@clipshelf.test")
        link_b = self._link("member-b@clipshelf.test")
        # The member switches to A and joins through an invitation to A; the
        # redemption spends only A's other tokens, so the link to B stays live.
        self._make_primary("member-a@clipshelf.test")
        self._invite("member-a@clipshelf.test")
        link_a = self._link("member-a@clipshelf.test")
        self.assertEqual(self.member_client.get(link_a).status_code, 200)
        self.assertTrue(self._is_member())
        response = self.owner_client.delete(
            f"/api/collections/{self.shared.id}/members/{self.member.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._is_member())
        # The removed member makes B primary again and opens the old link.
        self._make_primary("member-b@clipshelf.test")
        response = self.member_client.get(link_b)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self._is_member())
        self.assertContains(response, "no longer valid")


class AdminUserListVerificationTests(AccountTestCase):
    def test_user_list_reports_verification_status_per_user(self):
        verified_user = make_user("verified-list@clipshelf.test", verified=True)
        unverified_user = make_user("unverified-list@clipshelf.test", verified=False)
        self.client.force_login(self.admin)
        response = self.client.get("/api/admin/users")
        self.assertEqual(response.status_code, 200)
        users = {u["email"]: u for u in response.json()["users"]}
        self.assertIs(users[verified_user.email]["is_verified"], True)
        self.assertIs(users[unverified_user.email]["is_verified"], False)


class AdminCollectionListShapeTests(AccountTestCase):
    def test_collection_list_exposes_owner_and_members_with_exact_keys(self):
        owner = make_user("coll-owner@clipshelf.test")
        member = make_user("coll-member@clipshelf.test")
        shared = Collection(name="SharedListed", kind="shared", owner=owner)
        shared.save()
        Membership(collection=shared, user=member).save()
        self.client.force_login(self.admin)
        response = self.client.get("/api/admin/collections")
        self.assertEqual(response.status_code, 200)
        data = response.json()["collections"]
        self.assertEqual(
            data[0]["owner"],
            {"id": str(owner.pk), "email": owner.email, "is_active": True},
        )
        self.assertEqual(
            data[0]["members"],
            [{"id": str(member.pk), "email": member.email, "is_active": True}],
        )


_NO_CSRF_MIDDLEWARE = [
    m for m in settings.MIDDLEWARE if m != "django.middleware.csrf.CsrfViewMiddleware"
]


@override_settings(ROOT_URLCONF="clipshelf.urls", TEMPLATES=TEMPLATES)
class SameSiteApiCsrfTests(AccountTestCase):
    """Security-audit lead same-site-cookie-csrf-default-off, fixed: a page
    on another port of the same host is same-site, so the browser sends the
    Lax session cookie, and a text/plain POST needs no CORS preflight.
    api_csrf now answers 403 for cookie requests that are not same-origin
    or that carry a body without application/json. Adapted from CLIP-123
    commit 57c8f36, which asserted the unfixed behavior: its default test
    expected 201 and a planted FollowedSource row."""

    def setUp(self):
        self.victim = make_user("victim@clipshelf.test")
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.victim)

    def post_follow(self):
        return self.client.post(
            "/api/follows",
            data=json.dumps(
                {"url": "https://www.tiktok.com/@attacker", "tag": "planted"}
            ),
            content_type="text/plain",
            HTTP_HOST="192.168.1.10:8000",
            HTTP_ORIGIN="http://192.168.1.10:9000",
            HTTP_SEC_FETCH_SITE="same-site",
        )

    @override_settings(CLIPSHELF_CSRF=False, MIDDLEWARE=_NO_CSRF_MIDDLEWARE)
    def test_default_rejects_same_site_text_plain_cookie_post(self):
        response = self.post_follow()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            FollowedSource.objects.filter(user=self.victim, tag="planted").exists()
        )

    @override_settings(CLIPSHELF_CSRF=True, MIDDLEWARE=_CSRF_MIDDLEWARE)
    def test_csrf_flag_rejects_same_site_text_plain_cookie_post(self):
        response = self.post_follow()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FollowedSource.objects.filter(user=self.victim).exists())

    @override_settings(CLIPSHELF_CSRF=False, MIDDLEWARE=_NO_CSRF_MIDDLEWARE)
    def test_same_origin_json_cookie_post_still_creates_follow(self):
        response = self.client.post(
            "/api/follows",
            data=json.dumps({"url": "https://www.tiktok.com/@victim", "tag": "ui"}),
            content_type="application/json",
            HTTP_HOST="192.168.1.10:8000",
            HTTP_ORIGIN="http://192.168.1.10:8000",
            HTTP_SEC_FETCH_SITE="same-origin",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            FollowedSource.objects.filter(user=self.victim, tag="ui").exists()
        )

    @override_settings(CLIPSHELF_CSRF=False, MIDDLEWARE=_NO_CSRF_MIDDLEWARE)
    def test_token_post_skips_same_site_checks(self):
        client = Client(enforce_csrf_checks=True)
        token = self.headless_login(
            self.victim.email, client=client
        ).json()["meta"]["session_token"]
        response = client.post(
            "/api/follows",
            data=json.dumps(
                {"url": "https://www.tiktok.com/@app", "tag": "token"}
            ),
            content_type="text/plain",
            headers={"x-session-token": token},
            HTTP_HOST="192.168.1.10:8000",
            HTTP_ORIGIN="http://192.168.1.10:9000",
            HTTP_SEC_FETCH_SITE="cross-site",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            FollowedSource.objects.filter(user=self.victim, tag="token").exists()
        )

    @override_settings(CLIPSHELF_CSRF=False, MIDDLEWARE=_NO_CSRF_MIDDLEWARE)
    def test_same_origin_multipart_import_reaches_the_view(self):
        response = self.client.post(
            "/api/import",
            data={"client_request_id": str(uuid4())},
            format="multipart",
            HTTP_HOST="192.168.1.10:8000",
            HTTP_ORIGIN="http://192.168.1.10:8000",
            HTTP_SEC_FETCH_SITE="same-origin",
        )
        # The multipart type is allowed on the api_multipart-marked upload
        # view; this 400 comes from the view's own file check, not the gate.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "multipart file required"})

    @override_settings(CLIPSHELF_CSRF=False, MIDDLEWARE=_NO_CSRF_MIDDLEWARE)
    def test_cross_site_multipart_import_rejected(self):
        response = self.client.post(
            "/api/import",
            data={"client_request_id": str(uuid4())},
            format="multipart",
            HTTP_HOST="192.168.1.10:8000",
            HTTP_ORIGIN="http://192.168.1.10:9000",
            HTTP_SEC_FETCH_SITE="same-site",
        )
        self.assertEqual(response.status_code, 403)
