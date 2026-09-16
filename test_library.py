"""HTTP-observable defenses for the public API: privacy, auth boundaries,
idempotency/conflict, destination fallback, contributor-local removal.

Real database, real views, real authorization — no mocks.
"""

import json
import os
import tempfile
import uuid

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from allauth.account.models import EmailAddress

from clipshelf import services
from clipshelf.models import ImportRecord
from clipshelf.models import Asset, Capture, Collection, Contribution, Entry, Job, Membership

User = get_user_model()
PASSWORD = "test-pass-1234"


class ApiTestCase(TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="clipshelf-test-")
        override = override_settings(DATA_DIR=tmp)
        override.enable()
        self.addCleanup(override.disable)
        self.alice = self._user("alice@example.com")
        self.bob = self._user("bob@example.com")
        self.alice_personal = services.personal_collection(self.alice)
        self.bob_personal = services.personal_collection(self.bob)
        self.instance_id = str(services.get_settings().instance_id)

    def _user(self, email):
        user = User.objects.create_user(
            username=email, email=email, password=PASSWORD
        )
        # api_user requires a verified mailbox; unverified accounts stay locked out.
        EmailAddress.objects.create(
            user=user, email=email, verified=True, primary=True
        )
        return user

    def _login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def _contribute(self, user, collection, url="https://example.com/widget",
                    asset_ct="text/html"):
        capture = Capture.objects.create(
            user=user,
            client_request_id=uuid.uuid4(),
            raw_text=url,
            requested_collection_id=collection.id,
            collection=collection,
            request_hash="h",
            received_at=timezone.now(),
            notice="",
        )
        job = Job.objects.create(
            capture=capture,
            collection=collection,
            url=url,
            state="done",
            acquisition="complete",
            interpretation="complete",
            attempts=1,
            warnings=[],
        )
        entry, _ = Entry.objects.get_or_create(
            collection=collection, kind="link", key=url
        )
        Contribution.objects.create(
            entry=entry, user=user, capture=capture, job=job, origin="test", data={}
        )
        if asset_ct:
            rel = f"jobs/{job.id}/page.html"
            full = os.path.join(os.path.realpath(self._data_dir()), rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("<html><body>retained page</body></html>")
            Asset.objects.create(
                job=job,
                collection=collection,
                path=rel,
                kind="page",
                content_type=asset_ct,
                size=37,
                sha256="a" * 64,
                position=0,
            )
        return entry, job

    def _data_dir(self):
        from django.conf import settings

        return settings.DATA_DIR

    def _capture_body(self, user, collection=None, text=None, **overrides):
        body = {
            "client_request_id": str(uuid.uuid4()),
            "text": text or "look at https://example.com/widget",
            "collection_id": str(collection.id) if collection else None,
            "instance_id": self.instance_id,
            "user_id": str(user.id),
        }
        body.update(overrides)
        return body


class AuthenticationBoundaryTests(ApiTestCase):
    def test_unauthenticated_reads_get_json_401(self):
        client = Client()
        for path in (
            "/api/me",
            "/api/collections",
            "/api/entries",
            "/api/captures",
            "/api/import",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertIn("application/json", response["Content-Type"])
            self.assertIn(b"error", response.content)
            self.assertFalse(response.content.lstrip().startswith(b"<"))

    def test_invalid_session_token_never_falls_back_to_cookie(self):
        client = self._login(self.alice)
        response = client.get("/api/me", HTTP_X_SESSION_TOKEN="forged-token")
        self.assertEqual(response.status_code, 401)
        self.assertIn("application/json", response["Content-Type"])

    def test_cookie_mutation_without_csrf_succeeds_by_default(self):
        # Deliberate Jellyfin-style default: plain HTTP on a LAN behind a VPN;
        # the Lax HttpOnly session cookie is the mutation boundary, not tokens.
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(
            "/api/collections",
            data=json.dumps({"name": "Shop"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

    @override_settings(CLIPSHELF_CSRF=True)
    def test_cookie_mutation_without_csrf_is_403_when_csrf_enabled(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(
            "/api/collections",
            data=json.dumps({"name": "Shop"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("application/json", response["Content-Type"])
        # Token requests skip CSRF only when the token itself validates.
        response = client.post(
            "/api/collections",
            data=json.dumps({"name": "Shop"}),
            content_type="application/json",
            HTTP_X_SESSION_TOKEN="forged-token",
        )
        self.assertEqual(response.status_code, 401)

    def test_forged_headers_grant_nothing(self):
        client = Client()
        response = client.get(
            "/api/me",
            HTTP_X_SESSION_TOKEN="forged",
            HTTP_X_FORWARDED_USER="alice@example.com",
            HTTP_X_REMOTE_USER="alice@example.com",
        )
        self.assertEqual(response.status_code, 401)


class PrivacyTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.entry, self.job = self._contribute(self.alice, self.alice_personal)
        self.asset = Asset.objects.get(job=self.job)

    def test_personal_library_invisible_to_other_users(self):
        bob = self._login(self.bob)
        response = bob.get("/api/entries")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"], [])
        self.assertEqual(response.json()["count"], 0)
        response = bob.get(
            "/api/entries", {"collection_id": str(self.alice_personal.id)}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(bob.get(f"/api/entries/{self.entry.id}").status_code, 404)
        self.assertEqual(bob.get(f"/api/assets/{self.asset.id}").status_code, 404)
        self.assertEqual(
            bob.get(f"/api/captures/{self.job.capture_id}").status_code, 404
        )

    def test_counts_reveal_nothing_about_others(self):
        bob = self._login(self.bob)
        payload = bob.get("/api/entries").json()
        self.assertEqual(payload["count"], 0)
        me = bob.get("/api/me").json()
        self.assertNotIn(str(self.alice_personal.id), json.dumps(me))

    def test_no_store_on_private_reads(self):
        alice = self._login(self.alice)
        for path in ("/api/me", "/api/entries", f"/api/assets/{self.asset.id}"):
            response = alice.get(path)
            self.assertEqual(response["Cache-Control"], "no-store", path)

    def test_retained_html_served_inert(self):
        alice = self._login(self.alice)
        response = alice.get(f"/api/assets/{self.asset.id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        body = b"".join(response.streaming_content)
        self.assertNotIn(str(self._data_dir()).encode(), body)

    def test_asset_requires_remaining_contribution(self):
        Contribution.objects.filter(job=self.job).delete()
        alice = self._login(self.alice)
        response = alice.get(f"/api/assets/{self.asset.id}")
        self.assertEqual(response.status_code, 404)


class CollectionMembershipTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.alice_client = self._login(self.alice)
        self.bob_client = self._login(self.bob)

    def _shared(self):
        response = self.alice_client.post(
            "/api/collections",
            data=json.dumps({"name": "Team finds"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        return Collection.objects.get(id=response.json()["collection"]["id"])

    def test_member_add_remove_and_permissions(self):
        shared = self._shared()
        # Non-owner cannot manage members.
        response = self.bob_client.post(
            f"/api/collections/{shared.id}/members",
            data=json.dumps({"email": self.bob.email}),
            content_type="application/json",
        )
        # Non-members are told nothing about a collection they cannot see.
        self.assertEqual(response.status_code, 404)
        response = self.alice_client.post(
            f"/api/collections/{shared.id}/members",
            data=json.dumps({"email": self.bob.email}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        # Duplicate membership conflicts; unknown email rejected.
        self.assertEqual(
            self.alice_client.post(
                f"/api/collections/{shared.id}/members",
                data=json.dumps({"email": self.bob.email}),
                content_type="application/json",
            ).status_code,
            409,
        )
        self.assertEqual(
            self.alice_client.post(
                f"/api/collections/{shared.id}/members",
                data=json.dumps({"email": "nobody@example.com"}),
                content_type="application/json",
            ).status_code,
            400,
        )
        detail = self.alice_client.get(f"/api/collections/{shared.id}").json()
        emails = {m["email"] for m in detail["members"]}
        self.assertEqual(emails, {self.alice.email, self.bob.email})
        self.assertTrue(
            next(m for m in detail["members"] if m["email"] == self.alice.email)[
                "is_owner"
            ]
        )

    def test_member_removal_by_owner(self):
        shared = self._shared()
        Membership.objects.create(collection=shared, user=self.bob)
        response = self.alice_client.delete(
            f"/api/collections/{shared.id}/members/{self.bob.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Membership.objects.filter(collection=shared, user=self.bob).exists()
        )
        # Owner cannot be removed through this endpoint.
        Membership.objects.create(collection=shared, user=self.bob)
        self.assertEqual(
            self.alice_client.delete(
                f"/api/collections/{shared.id}/members/{self.alice.id}"
            ).status_code,
            400,
        )
        # Non-owner member cannot remove anyone.
        self.assertEqual(
            self.bob_client.delete(
                f"/api/collections/{shared.id}/members/{self.alice.id}"
            ).status_code,
            403,
        )

    def test_personal_collection_has_no_member_management(self):
        response = self.alice_client.post(
            f"/api/collections/{self.alice_personal.id}/members",
            data=json.dumps({"email": self.bob.email}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_settings_rejects_non_writable_default(self):
        response = self.bob_client.post(
            "/api/settings",
            data=json.dumps({"default_collection_id": str(self.alice_personal.id)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        response = self.alice_client.post(
            "/api/settings",
            data=json.dumps({"default_collection_id": str(self.alice_personal.id)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["default_collection_id"], str(self.alice_personal.id)
        )


class RemovalTests(ApiTestCase):
    def test_contributor_removal_preserves_others(self):
        shared = Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice
        )
        Membership.objects.create(collection=shared, user=self.bob)
        entry_a, job_a = self._contribute(self.alice, shared)
        entry_b, job_b = self._contribute(self.bob, shared)
        self.assertEqual(entry_a.id, entry_b.id)  # same URL, one entry
        asset_a = Asset.objects.get(job=job_a)

        response = self._login(self.bob).delete(f"/api/entries/{entry_a.id}")
        self.assertEqual(response.status_code, 200)
        # Alice's contribution survives; bob lost access to alice's asset via
        # his own removed contribution but keeps membership access.
        alice = self._login(self.alice)
        self.assertEqual(
            alice.get(f"/api/entries/{entry_a.id}").status_code, 200
        )
        self.assertEqual(
            alice.get(f"/api/assets/{asset_a.id}").status_code, 200
        )
        self.assertTrue(
            Contribution.objects.filter(entry=entry_a, user=self.alice).exists()
        )
        self.assertFalse(
            Contribution.objects.filter(entry=entry_a, user=self.bob).exists()
        )

    def test_sole_contributor_removal_removes_entry(self):
        shared = Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice
        )
        entry, _ = self._contribute(self.alice, shared)
        response = self._login(self.alice).delete(f"/api/entries/{entry.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._login(self.alice).get(f"/api/entries/{entry.id}").status_code,
            404,
        )

    def test_owner_may_moderate_member_contribution(self):
        shared = Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice
        )
        Membership.objects.create(collection=shared, user=self.bob)
        entry, _ = self._contribute(self.bob, shared)
        response = self._login(self.alice).delete(f"/api/entries/{entry.id}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Contribution.objects.filter(entry=entry).exists()
        )


class CaptureTests(ApiTestCase):
    def _post_capture(self, client, body):
        return client.post(
            "/api/captures",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_idempotent_retry_and_conflict(self):
        alice = self._login(self.alice)
        body = self._capture_body(self.alice, self.alice_personal)
        first = self._post_capture(alice, body)
        self.assertEqual(first.status_code, 201)
        receipt = first.json()["receipt"]
        self.assertEqual(receipt["collection_id"], str(self.alice_personal.id))
        self.assertEqual(
            Capture.objects.filter(user=self.alice).count(), 1
        )
        second = self._post_capture(alice, body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["receipt"]["id"], receipt["id"])
        self.assertEqual(Capture.objects.filter(user=self.alice).count(), 1)
        conflict = self._post_capture(
            alice, dict(body, text="different https://example.com/other")
        )
        self.assertEqual(conflict.status_code, 409)

    def test_identity_validation(self):
        alice = self._login(self.alice)
        response = self._post_capture(
            alice,
            self._capture_body(self.alice, self.alice_personal, instance_id=str(uuid.uuid4())),
        )
        self.assertEqual(response.status_code, 409)
        response = self._post_capture(
            alice,
            self._capture_body(self.alice, self.alice_personal, user_id=str(self.bob.id)),
        )
        self.assertEqual(response.status_code, 409)

    def test_text_limits(self):
        alice = self._login(self.alice)
        self.assertEqual(
            self._post_capture(alice, self._capture_body(self.alice, text="no urls here")).status_code,
            400,
        )
        many = " ".join(f"https://example.com/{i}" for i in range(51))
        self.assertEqual(
            self._post_capture(alice, self._capture_body(self.alice, text=many)).status_code,
            400,
        )
        big = "https://example.com/x " + "y" * 33000
        self.assertEqual(
            self._post_capture(alice, self._capture_body(self.alice, text=big)).status_code,
            413,
        )

    def test_unavailable_destination_falls_back_with_notice(self):
        shared = Collection.objects.create(
            name="Not mine", kind="shared", owner=self.alice
        )
        bob = self._login(self.bob)
        response = self._post_capture(
            bob, self._capture_body(self.bob, shared)
        )
        self.assertEqual(response.status_code, 201)
        receipt = response.json()["receipt"]
        self.assertEqual(receipt["collection_id"], str(self.bob_personal.id))
        self.assertTrue(receipt.get("notice"))

    def test_inbox_scoped_and_authorized(self):
        alice = self._login(self.alice)
        body = self._capture_body(self.alice, self.alice_personal)
        first = self._post_capture(alice, body)
        capture_id = first.json()["receipt"]["id"]
        listing = alice.get("/api/captures").json()
        self.assertEqual([c["id"] for c in listing["captures"]], [capture_id])
        detail = alice.get(f"/api/captures/{capture_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["receipt"]["id"], capture_id)
        # Other users cannot see the capture; wrong collection scoping 404s.
        self.assertEqual(
            self._login(self.bob).get(f"/api/captures/{capture_id}").status_code,
            404,
        )
        response = alice.get(
            "/api/captures", {"collection_id": str(self.bob_personal.id)}
        )
        self.assertEqual(response.status_code, 404)

    def test_job_retry_rules(self):
        alice = self._login(self.alice)
        body = self._capture_body(self.alice, self.alice_personal)
        response = self._post_capture(alice, body)
        capture_id = response.json()["receipt"]["id"]
        detail = alice.get(f"/api/captures/{capture_id}").json()
        job = detail["jobs"][0]
        job_row = Job.objects.get(id=job["id"])
        job_row.state = "blocked"
        job_row.error = "upstream blocked"
        job_row.save()

        # Other users cannot retry.
        self.assertEqual(
            self._login(self.bob).post(f"/api/jobs/{job['id']}/retry").status_code,
            404,
        )
        # In-flight job refuses retry.
        job_row.state = "queued"
        job_row.save()
        self.assertEqual(
            alice.post(f"/api/jobs/{job['id']}/retry").status_code, 409
        )
        # Blocked job requeues keeping history.
        job_row.state = "blocked"
        job_row.save()
        response = alice.post(f"/api/jobs/{job['id']}/retry")
        self.assertEqual(response.status_code, 200)
        job_row.refresh_from_db()
        self.assertEqual(job_row.state, "queued")
        self.assertEqual(job_row.error, "upstream blocked")


class EntryApiTests(ApiTestCase):
    def test_query_validation(self):
        alice = self._login(self.alice)
        self.assertEqual(
            alice.get("/api/entries", {"kind": "bogus"}).status_code, 400
        )
        self.assertEqual(
            alice.get("/api/entries", {"limit": "0"}).status_code, 400
        )
        self.assertEqual(
            alice.get("/api/entries", {"limit": "abc"}).status_code, 400
        )

    def test_member_reads_shared_entry(self):
        shared = Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice
        )
        Membership.objects.create(collection=shared, user=self.bob)
        entry, _ = self._contribute(self.alice, shared)
        payload = self._login(self.bob).get(f"/api/entries/{entry.id}").json()
        self.assertEqual(payload["entry"]["id"], str(entry.id))
        self.assertTrue(payload["assets"])
        listing = self._login(self.bob).get(
            "/api/entries", {"collection_id": str(shared.id)}
        ).json()
        self.assertEqual(listing["count"], 1)


class ImportTests(ApiTestCase):
    def _post_import(self, client, content, crid=None, collection=None,
                     user=None, **extra):
        actor = user or self.alice
        destination = collection or services.personal_collection(actor)
        data = {
            "client_request_id": crid or str(uuid.uuid4()),
            "collection_id": str(destination.id),
            "instance_id": self.instance_id,
            "user_id": str(actor.id),
        }
        data.update(extra)
        data["file"] = SimpleUploadedFile(
            "tiktok.json", content, content_type="application/json"
        )
        return client.post("/api/import", data=data)

    def test_import_idempotency_and_conflict(self):
        alice = self._login(self.alice)
        crid = str(uuid.uuid4())
        content = json.dumps([{"id": "1", "desc": "clip one"}]).encode()
        first = self._post_import(alice, content, crid=crid)
        self.assertEqual(first.status_code, 201)
        record_id = first.json()["import"]["id"]
        staged = list(
            os.listdir(os.path.join(os.path.realpath(self._data_dir()), "staging"))
        )
        self.assertEqual(len(staged), 1)
        # Same bytes + same request id: idempotent.
        again = self._post_import(alice, content, crid=crid)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["import"]["id"], record_id)
        # Same request id + different bytes: conflict.
        self.assertEqual(
            self._post_import(alice, content + b" ", crid=crid).status_code, 409
        )
        repeat = self._post_import(alice, content, crid=str(uuid.uuid4()))
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json()["import"]["id"], record_id)

    def test_import_rejects_credentials_and_garbage(self):
        alice = self._login(self.alice)
        with_cookies = json.dumps([{"id": "1", "cookies": "sid=secret"}]).encode()
        response = self._post_import(alice, with_cookies)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImportRecord.objects.exists())
        response = self._post_import(alice, b'{"not": "a list"}')
        self.assertEqual(response.status_code, 400)
        response = self._post_import(alice, b"not json at all")
        self.assertEqual(response.status_code, 400)
        response = self._post_import(alice, json.dumps([{
            "url": "https://example.com/clip", "error": "offline",
            "cache": {"path": "/data/secret_key"},
        }]).encode())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ImportRecord.objects.exists())

    def test_import_enforces_ceiling(self):
        from django.test import override_settings as os_

        alice = self._login(self.alice)
        content = json.dumps([{"id": "1", "desc": "x" * 512}]).encode()
        with os_(CLIPSHELF_MAX_IMPORT_BYTES=256):
            response = self._post_import(alice, content)
        self.assertEqual(response.status_code, 413)

    def test_import_requires_writable_collection(self):
        alice_shared = Collection.objects.create(
            name="Alice only", kind="shared", owner=self.alice
        )
        bob = self._login(self.bob)
        response = self._post_import(
            bob,
            json.dumps([{"id": "1"}]).encode(),
            collection=alice_shared,
            user=self.bob,
        )
        self.assertIn(response.status_code, (403, 404))


class ShellAndHealthTests(ApiTestCase):
    def test_healthz_reports_real_database(self):
        response = Client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(response.json()["database"])

    def test_root_redirects_anonymous_and_renders_shell(self):
        response = Client().get("/")
        self.assertEqual(response.status_code, 302)
        response = self._login(self.alice).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_static_assets_are_served(self):
        # Tests run with DEBUG=False, which is the path the container uses:
        # the stylesheet the shell links must come back, not a 500 or a 404.
        response = Client().get("/static/clipshelf/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content).strip())

    def test_static_refuses_to_escape_its_directory(self):
        response = Client().get("/static/../db.sqlite3")
        self.assertNotEqual(response.status_code, 200)

    def test_pinned_hosts_still_answer_the_container_probe(self):
        # Pinning real hostnames must not make the app reject its own
        # healthcheck, which reaches it as Host: 127.0.0.1:8000.
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as data_dir:
            env = {k: v for k, v in os.environ.items() if not k.startswith("CLIPSHELF_")}
            env.update(
                CLIPSHELF_ALLOWED_HOSTS="nas.example,nas.tail1234.ts.net",
                CLIPSHELF_DATA_DIR=data_dir,
                # Unrelated deployment gate: runners ship an older SQLite and
                # this asserts host derivation, not the runtime build.
                CLIPSHELF_SQLITE_VERIFIED="1",
                DJANGO_SETTINGS_MODULE="clipshelf.project.settings",
            )
            code = (
                "import json, django; django.setup();"
                "from django.conf import settings;"
                "print(json.dumps(settings.ALLOWED_HOSTS))"
            )
            out = subprocess.run(
                [sys.executable, "-c", code], env=env, capture_output=True, text=True
            )
        self.assertEqual(out.returncode, 0, out.stderr)
        hosts = json.loads(out.stdout)
        self.assertEqual(hosts[0], "nas.example")
        self.assertIn("127.0.0.1", hosts)
