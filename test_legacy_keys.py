"""Legacy GitHub key coexistence: case-preserving keys must not duplicate
entries or tombstones shipped under the old all-lowercase scheme, dedup
stays collection-scoped, and no stored key is ever rewritten. The policy
mirrors the prompt SHA-1/SHA-256 dual identity: legacy forms are recognized
at every lookup, never minted, never migrated."""
import shutil
import tempfile
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from clipshelf import lib, models, publication, services, worker

User = get_user_model()

CASED = "https://github.com/foo/bar/blob/Main/ReadMe.md"
LEGACY = "https://github.com/foo/bar/blob/main/readme.md"


class KeyCaseTests(TestCase):
    """The canonicalization premise and the recognized identity set."""

    def test_norm_preserves_deep_github_case(self):
        # owner/repo are case-insensitive on GitHub and still fold
        self.assertEqual(lib.norm("https://GitHub.com/Foo/Bar"),
                         "https://github.com/foo/bar")
        # ref and file path case is preserved from now on
        self.assertEqual(publication.link_key(CASED), CASED)
        self.assertNotEqual(publication.link_key(CASED),
                            publication.link_key(LEGACY))

    def test_legacy_identity_recognized_never_minted(self):
        self.assertEqual(publication.legacy_link_key(CASED), LEGACY)
        # a legacy key is its own legacy form: norm is idempotent on it
        self.assertEqual(publication.legacy_link_key(LEGACY), LEGACY)
        # non-GitHub paths never had a lowercase rule
        self.assertEqual(publication.legacy_link_key("https://example.com/Case"),
                         "https://example.com/Case")
        # the recognized set covers both forms for a cased input
        self.assertEqual(publication.link_keys(CASED), {CASED, LEGACY})
        self.assertEqual(publication.link_keys(LEGACY), {LEGACY})


class PublicationIdentityTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="a", email="a@example.com")
        self.personal = services.personal_collection(self.alice)

    def _job(self, url):
        capture = models.Capture.objects.create(
            user=self.alice, client_request_id=uuid.uuid4(), raw_text=url,
            requested_collection_id=self.personal.id, collection=self.personal,
            request_hash="h", received_at=timezone.now(), notice="")
        return models.Job.objects.create(
            capture=capture, collection=self.personal, url=url,
            state="queued", acquisition="complete", interpretation="pending",
            attempts=1, warnings=[])

    def test_cased_contribution_reuses_legacy_entry_without_rewrite(self):
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind="link", key=LEGACY)
        publication.contribute_link(
            collection=self.personal, user=self.alice, capture=None, job=None,
            key=CASED, data={"title": "t"})
        entries = models.Entry.objects.filter(collection=self.personal, kind="link")
        self.assertEqual(entries.count(), 1, "cased re-add duplicated the legacy entry")
        self.assertEqual(entries.get().id, legacy_entry.id)
        self.assertEqual(entries.get().key, LEGACY, "legacy key was rewritten")
        self.assertTrue(legacy_entry.contributions.exists())

    def test_link_entry_reuses_legacy_identity(self):
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind="link", key=LEGACY)
        entry, created = publication.link_entry(self.personal, CASED)
        self.assertFalse(created)
        self.assertEqual(entry.id, legacy_entry.id)
        entry, created = publication.link_entry(self.personal, CASED)
        self.assertEqual(models.Entry.objects.filter(kind="link").count(), 1)
        entry, created = publication.link_entry(self.personal, LEGACY)
        self.assertEqual(entry.id, legacy_entry.id)

    def test_variant_reuse_is_collection_scoped(self):
        shared = models.Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice)
        models.Membership.objects.create(collection=shared, user=self.alice)
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind="link", key=LEGACY)
        entry, _ = publication.link_entry(shared, CASED)
        self.assertEqual(models.Entry.objects.filter(kind="link").count(), 2)
        self.assertEqual(entry.collection_id, shared.id,
                         "identity leaked across collections")
        self.assertEqual(entry.key, CASED)
        self.assertNotEqual(entry.id, legacy_entry.id)

    def test_legacy_tombstone_blocks_cased_readd_and_is_retained(self):
        models.History.objects.create(
            collection=self.personal, user=self.alice,
            kind=models.History.Kind.REMOVED, url=LEGACY)
        job = self._job(CASED)
        services.store_source(job, {"url": CASED, "acquisition": "complete",
                                    "title": "t"})
        self.assertFalse(
            models.Entry.objects.filter(collection=self.personal).exists(),
            "cased re-add resurrected a legacy-removed link")
        self.assertTrue(
            publication.tombstoned(self.personal.id, self.alice.id,
                                   *publication.link_keys(CASED)),
            "cased identity no longer matches the legacy tombstone")
        row = models.History.objects.get(
            collection=self.personal, kind=models.History.Kind.REMOVED)
        self.assertEqual(row.url, LEGACY, "tombstone row was rewritten")

    def test_store_source_tombstone_check_accepts_redirect_variants(self):
        models.History.objects.create(
            collection=self.personal, user=self.alice,
            kind=models.History.Kind.REMOVED, url=LEGACY)
        job = self._job(CASED)
        services.store_findings(job, {"source": CASED, "prompts": []})
        self.assertFalse(
            models.Entry.objects.filter(collection=self.personal).exists())
        self.assertIn("removed from this collection", " ".join(job.warnings))

    def test_tombstone_scoping_across_users_and_collections(self):
        bob = User.objects.create_user(username="b", email="b@example.com")
        shared = models.Collection.objects.create(
            name="Shared", kind="shared", owner=self.alice)
        models.Membership.objects.create(collection=shared, user=self.alice)
        models.Membership.objects.create(collection=shared, user=bob)
        models.History.objects.create(
            collection=self.personal, user=self.alice,
            kind=models.History.Kind.REMOVED, url=LEGACY)
        self.assertFalse(publication.tombstoned(
            shared.id, self.alice.id, *publication.link_keys(CASED)),
            "personal tombstone leaked into the shared collection")
        entry, _ = publication.link_entry(shared, CASED)
        self.assertEqual(entry.key, CASED)
        self.assertTrue(publication.tombstoned(
            self.personal.id, self.alice.id, *publication.link_keys(CASED)))
        self.assertFalse(publication.tombstoned(
            self.personal.id, bob.id, *publication.link_keys(CASED)),
            "alice's tombstone suppressed bob")


class MergeVariantTests(TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="clipshelf-keys-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        override = override_settings(DATA_DIR=tmp)
        override.enable()
        self.addCleanup(override.disable)
        self.alice = User.objects.create_user(username="m", email="m@example.com")
        self.personal = services.personal_collection(self.alice)

    def test_merge_legacy_tombstone_blocks_cased_link(self):
        models.History.objects.create(
            collection=self.personal, user=self.alice,
            kind=models.History.Kind.REMOVED, url=LEGACY)
        stats = worker.merge_findings(
            self.alice, [{"source": CASED, "links": [{"url": CASED, "title": "t"}]}])
        self.assertEqual(stats["links"], 0, "extracted merge resurrected a removed link")
        self.assertFalse(
            models.Entry.objects.filter(collection=self.personal, kind="link").exists())

    def test_merge_reuses_legacy_entry(self):
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind="link", key=LEGACY)
        worker.merge_findings(
            self.alice, [{"source": CASED, "links": [{"url": CASED, "title": "t"}]}])
        self.assertEqual(
            models.Entry.objects.filter(collection=self.personal, kind="link").count(), 1)
        self.assertTrue(legacy_entry.contributions.exists())


class ImportVariantTests(TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="clipshelf-keys-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        override = override_settings(DATA_DIR=tmp)
        override.enable()
        self.addCleanup(override.disable)
        self.tmp = tmp
        self.user = User.objects.create_user(username="i", email="i@example.com")
        self.personal = services.personal_collection(self.user)

    def _import(self, items, removed=None):
        def fake_import_export(batch, directory):
            return [{"url": item["url"], "original_url": item["url"], "title": "",
                     "desc": "", "text": "", "links": [], "assets": [],
                     "acquisition": "complete", "warnings": [], "metadata": {}}
                    for item in batch]
        with mock.patch("clipshelf.acquisition.import_export",
                        side_effect=fake_import_export):
            return worker.import_items(user=self.user, collection_id=None,
                                       items=items, removed=removed or {},
                                       cache_dir=self.tmp)

    def test_import_cased_item_reuses_legacy_entry(self):
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind="link", key=LEGACY)
        result = self._import([{"url": CASED}])
        self.assertEqual(result["counts"]["entries"], 1)
        entries = models.Entry.objects.filter(collection=self.personal, kind="link")
        self.assertEqual(entries.count(), 1, "import duplicated the legacy entry")
        self.assertEqual(entries.get().id, legacy_entry.id)
        self.assertTrue(models.Contribution.objects.filter(entry=legacy_entry).exists())

    def test_import_legacy_removed_blocks_cased_item(self):
        result = self._import([{"url": CASED}], removed={LEGACY: "2024-01-01"})
        self.assertEqual(result["counts"]["entries"], 0)
        self.assertFalse(models.Entry.objects.exists(),
                         "import resurrected a removed link under its cased key")
