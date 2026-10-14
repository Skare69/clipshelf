"""Legacy migration regressions: manifest counts/digests, idempotency, scope,
settings recording, and backup/restore roundtrip."""
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, TestCase, TransactionTestCase, override_settings
from allauth.account.models import EmailAddress
from PIL import Image

from django.conf import settings

from clipshelf import acquisition, cli, imports, judgment, lib, models, publication, services, worker
from clipshelf.management.commands.backup import CONFIG_KEYS, Command as BackupCommand

CACHE_BYTES = b"<html>cached page</html>"
CACHE_NAME = "cache/" + hashlib.sha1(CACHE_BYTES).hexdigest() + ".html"


def library_dict():
    return {
        "seen": {"https://seen.example/1": "2024-01-01"},
        "links": {
            "https://github.com/a/b": {
                "title": "Repo A", "desc": "", "sources": [], "tags": ["github"],
                "found": "2024-01-01", "interpreted": "2024-02-01",
                "cat": "ml", "install": "pip install x", "cache": CACHE_NAME},
            "https://example.com/pending": {
                "title": "P", "desc": "pending desc", "sources": ["dump.txt"],
                "tags": [], "found": "2024-01-02", "pending": True},
            "https://removed.example/x": {
                "title": "R", "desc": "", "sources": [], "tags": [], "found": "2024-01-02"},
        },
        "prompts": {"abc123": {"title": "Prompt title", "text": "do a thing",
                               "sources": ["s"], "found": "2024-01-03",
                               "cat": "general", "interpreted": "2024-02-02"}},
        "removed": {"https://removed.example/x": "2024-01-05"},
        "settings": {"interpret_cmd": "omp -p --model old"},
    }


def fake_acquire(url, directory):
    """The fetch is the only network seam an import reaches: media specs and
    page caches are handled by the real acquisition.import_export itself."""
    source = acquisition.new_source(url)
    source["acquisition"] = "complete"
    return source


class MigrationMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clipshelf-mig-"))
        self.cache_dir = self.tmp / "cache"
        self.cache_dir.mkdir()
        (self.cache_dir / Path(CACHE_NAME).name).write_bytes(CACHE_BYTES)
        self.library_path = self.tmp / "library.json"
        self.library = library_dict()
        self.input_bytes = self.write_library()
        self.user = models.User.objects.create_user(username="m1", email="m1@example.com")
        self.personal = services.personal_collection(self.user)
        services.get_settings().save()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_library(self):
        payload = json.dumps(self.library).encode("utf-8")
        self.library_path.write_bytes(payload)
        return payload

    def run_import_command(self, *extra):
        out = io.StringIO()
        args = ["--user", "m1@example.com", "--input", str(self.library_path),
                "--cache", str(self.cache_dir), *extra]
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire",
                           side_effect=fake_acquire):
            call_command("import_library", *args, stdout=out)
        return out.getvalue()

    def direct_import(self, items=None, prompts=None, dry_run=False, **kw):
        """Direct imports.import_items call against this fixture's library."""
        items = items if items is not None else [
            {"url": u, **e} for u, e in self.library["links"].items()]
        prompts = prompts if prompts is not None else list(self.library["prompts"].values())
        kw.setdefault("cache_dir", str(self.cache_dir))
        kw.setdefault("dry_run", dry_run)
        kw.setdefault("seen", self.library["seen"])
        kw.setdefault("removed", self.library["removed"])
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire",
                           side_effect=fake_acquire):
            return imports.import_items(user=self.user, collection_id=None,
                                       items=items, prompts=prompts, **kw)


class ManifestTests(MigrationMixin, TestCase):
    def test_dry_run_manifest_matches_input_without_importing(self):
        result = self.direct_import(dry_run=True)
        manifest = result["manifest"]
        self.assertEqual(manifest["counts"], {
            "links": 2, "prompts": 1, "seen": 1, "removed": 1,
            "media_files": 1, "missing_media": 0})
        self.assertEqual(sorted(manifest["links"]),
                         ["https://example.com/pending", "https://github.com/a/b"])
        self.assertNotIn("https://removed.example/x", manifest["links"])
        self.assertEqual(manifest["media_files"][CACHE_NAME]["sha256"],
                         hashlib.sha256(CACHE_BYTES).hexdigest())
        self.assertTrue(all(v["fields_sha256"] for v in manifest["links"].values()))
        self.assertEqual(self.library_path.read_bytes(), self.input_bytes, "input mutated")
        self.assertEqual(models.Entry.objects.count(), 0, "dry run imported data")

    def test_dry_run_leaves_settings_unactivated(self):
        self.run_import_command("--dry-run")
        self.assertFalse(services.get_settings().llm_base_url,
                         "interpret_cmd must never become LLM config")

    def test_manifest_digests_stable_across_runs(self):
        first = self.direct_import(dry_run=True)["manifest"]
        second = self.direct_import(dry_run=True)["manifest"]
        self.assertEqual(first["links"], second["links"])
        self.assertEqual(first["prompts"], second["prompts"])
        self.assertEqual(first["media_files"], second["media_files"])

    def test_digest_changes_when_history_changes(self):
        baseline = self.direct_import(dry_run=True)["digest"]
        changed_seen = self.direct_import(
            dry_run=True, seen={"https://seen.example/1": "2024-02-01"})["digest"]
        changed_removed = self.direct_import(
            dry_run=True, removed={"https://removed.example/x": "2024-02-01"})["digest"]
        self.assertNotEqual(baseline, changed_seen)
        self.assertNotEqual(baseline, changed_removed)

    def test_changed_media_bytes_make_a_new_import(self):
        first = self.direct_import()
        media_path = self.cache_dir / Path(CACHE_NAME).name
        media_path.write_bytes(CACHE_BYTES + b" changed")
        second = self.direct_import()
        self.assertNotEqual(first["digest"], second["digest"])
        self.assertTrue(second["created"])
        self.assertEqual(models.ImportRecord.objects.filter(user=self.user).count(), 2)

    def test_legacy_fixture_digest_stable_across_resolver_unification(self):
        # The digest keys ImportRecord dedupe. cache_dir is part of the
        # digest, so no cross-run constant can pin it; instead the expected
        # value is computed with the pre-unification media walk (inlined
        # below) against this run's own cache_dir. An exact repeat of an
        # import made before the unification must still dedupe.
        items = imports._dedupe(
            [{"url": u, **e} for u, e in self.library["links"].items()],
            {lib.norm(url) or url for url in self.library["removed"]})
        h = hashlib.sha256()
        h.update(b"clipshelf-import-v2\n")
        for value in (items, list(self.library["prompts"].values()),
                      self.library["seen"], self.library["removed"]):
            h.update(imports._canonical(value).encode())
            h.update(b"\n")
        for item in items:
            images = item.get("images")
            values = [*(images if isinstance(images, list) else []),
                      item.get("video"), item.get("cache")]
            for value in values:
                path = value.get("path") if isinstance(value, dict) else value
                if (not isinstance(path, str) or not path
                        or path.startswith(("http://", "https://"))):
                    continue
                source = imports._legacy_media_path(
                    path.replace("\\", "/"), str(self.cache_dir))
                h.update(imports._canonical({
                    "path": path,
                    "sha256": imports._sha256_file(source) if source else None,
                }).encode())
                h.update(b"\n")
        h.update(str(self.cache_dir).encode())
        self.assertEqual(self.direct_import(dry_run=True)["digest"], h.hexdigest())

    def test_legacy_links_shapes_raise_validation_error(self):
        for links, message in (
                ([], "library.json links must be a JSON object"),
                ({"https://example.com": []},
                 "legacy link entries must be JSON objects")):
            with self.subTest(links=links), \
                    self.assertRaises(ValidationError) as raised:
                imports.legacy_payload({"links": links})
            self.assertEqual(raised.exception.messages, [message])

    def test_legacy_history_url_bound_uses_lib_max_url_length(self):
        # the bound must track lib.MAX_URL_LENGTH, not a copied 2048 literal
        with mock.patch.object(lib, "MAX_URL_LENGTH", 20):
            with self.assertRaises(ValidationError) as raised:
                self.direct_import(dry_run=True,
                                   seen={"x" * 21: "2024-01-01"})
        self.assertEqual(raised.exception.messages,
                         ["legacy history URLs must be non-empty strings up "
                          "to 20 characters"])


    def test_import_success_report_names_digest_and_counts(self):
        out = self.run_import_command().rstrip("\n")
        record = models.ImportRecord.objects.get(user=self.user)
        counts = record.manifest["counts"]
        # head and tail exact; result= must be followed by the result counts
        # repr (an appended x after any constant breaks head, tail, or brace)
        head, sep, tail = out.partition(" result=")
        self.assertEqual(head,
                         f"imported: digest={record.input_digest[:16]} "
                         f"counts={counts}")
        self.assertTrue(sep, out)
        self.assertTrue(tail.startswith("{"), tail)
        self.assertTrue(tail.endswith(
            f" into personal collection {self.personal.id} "
            "(interpret_cmd recorded, not activated)"), tail)


class MediaParityTests(MigrationMixin, TestCase):
    """The dry run and the real import must report the same media set for
    every reference shape: dict specs, padded paths, backslash paths."""

    def assert_parity(self, extra):
        items = [{"url": "https://parity.example/1", **extra}]
        dry = self.direct_import(items=items, dry_run=True)["manifest"]
        real = self.direct_import(items=items)["manifest"]
        self.assertEqual(dry["media_files"], real["media_files"])
        self.assertEqual(dry["missing_media"], real["missing_media"])
        return dry

    def test_dict_path_reference_matches_real_import(self):
        manifest = self.assert_parity({"cache": {"path": CACHE_NAME}})
        self.assertEqual(list(manifest["media_files"]), [CACHE_NAME])
        self.assertEqual(manifest["counts"]["missing_media"], 0)

    def test_padded_path_matches_real_import(self):
        manifest = self.assert_parity({"cache": CACHE_NAME + " "})
        self.assertEqual(list(manifest["media_files"]), [CACHE_NAME])
        self.assertEqual(manifest["counts"]["missing_media"], 0)

    def test_backslash_path_matches_real_import(self):
        manifest = self.assert_parity({"cache": CACHE_NAME.replace("/", "\\")})
        self.assertEqual(list(manifest["media_files"]), [CACHE_NAME])
        self.assertEqual(manifest["counts"]["missing_media"], 0)

    def test_missing_padded_reference_is_missing_in_both(self):
        manifest = self.assert_parity({"cache": "cache/gone.html "})
        self.assertEqual(manifest["missing_media"], ["cache/gone.html"])
        self.assertEqual(manifest["counts"]["media_files"], 0)


class ImportTests(MigrationMixin, TestCase):
    def test_import_creates_scoped_entries_assets_jobs(self):
        other = models.User.objects.create_user(username="m2", email="m2@example.com")
        other_personal = services.personal_collection(other)
        self.run_import_command()
        record = models.ImportRecord.objects.get(user=self.user)
        manifest = record.manifest
        self.assertEqual(manifest["legacy_settings"],
                         {"interpret_cmd": "omp -p --model old", "activated": False})
        self.assertEqual(manifest["counts"]["links"], 2)
        self.assertEqual(models.Entry.objects.filter(
            collection=self.personal, kind="link").count(), 2)
        self.assertEqual(models.Entry.objects.filter(
            collection=self.personal, kind="prompt").count(), 1)
        self.assertEqual(models.Entry.objects.filter(
            collection=other_personal).count(), 0, "import leaked to another account")

        done = models.Job.objects.get(url="https://github.com/a/b")
        self.assertEqual(done.state, "done")
        self.assertEqual(done.interpretation, "complete")
        self.assertEqual(done.findings["categories"], ["ml"])
        queued = models.Job.objects.get(url="https://example.com/pending")
        self.assertEqual(queued.state, "queued")
        self.assertEqual(queued.interpretation, "pending")

        asset = models.Asset.objects.get(job=done)
        published = Path(self.tmp) / asset.path
        self.assertEqual(published.read_bytes(), CACHE_BYTES, "asset bytes changed")
        self.assertTrue(str(asset.path).startswith("assets/"))

        contribution = models.Contribution.objects.get(
            entry__key="https://github.com/a/b", user=self.user,
            origin="legacy-import")
        self.assertEqual(contribution.data["install"], "pip install x")
        self.assertEqual(contribution.data["cat"], "ml")
        entries = (services.serialize_entry(entry, self.user)
                   for entry in models.Entry.objects.filter(collection=self.personal))
        statuses = {item["title"]: item["interpreted"] for item in entries}
        self.assertEqual(statuses, {"Repo A": True, "P": False, "Prompt title": True})

    def test_repeat_import_unchanged(self):
        self.run_import_command()
        entries_before = models.Entry.objects.count()
        second = self.run_import_command()
        self.assertIn("unchanged", second)
        self.assertEqual(models.ImportRecord.objects.filter(user=self.user).count(), 1)
        self.assertEqual(models.Entry.objects.count(), entries_before)

    def test_tombstones_not_imported(self):
        self.run_import_command()
        self.assertFalse(models.Entry.objects.filter(
            collection=self.personal, key="https://removed.example/x").exists())

    def test_import_history_is_scoped_alias_aware_and_idempotent(self):
        seen_alias = "https://seen.example/1/?utm_source=legacy#old"
        removed_alias = "https://removed.example/x/?utm_source=legacy#old"
        args = {
            "items": [{"url": removed_alias}],
            "prompts": [],
            "seen": {seen_alias: "2024-01-01"},
            "removed": {removed_alias: "2024-01-05"},
        }
        self.direct_import(**args)
        history = models.History.objects.filter(user=self.user)
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.SEEN,
            url="https://seen.example/1").exists())
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.SEEN,
            url=seen_alias).exists())
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.ALIAS,
            url=seen_alias, target_url="https://seen.example/1").exists())
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.REMOVED,
            url="https://removed.example/x").exists())
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.REMOVED,
            url=removed_alias).exists())
        self.assertTrue(history.filter(
            collection=self.personal, kind=models.History.Kind.ALIAS,
            url=removed_alias, target_url="https://removed.example/x").exists())
        self.assertFalse(models.Entry.objects.filter(
            collection=self.personal, key="https://removed.example/x").exists())
        other = models.User.objects.create_user(username="history-other",
                                                email="history-other@example.com")
        self.assertFalse(models.History.objects.filter(
            collection=services.personal_collection(other)).exists())
        count = history.count()
        self.direct_import(**args)
        self.assertEqual(history.count(), count)

        self.direct_import(items=[{"url": "https://removed.example/x"}],
                           prompts=[], seen={}, removed={})
        self.assertFalse(models.Entry.objects.filter(
            collection=self.personal, key="https://removed.example/x").exists())

    def test_legacy_github_tombstones_match_case_preserved_paths(self):
        other = models.User.objects.create_user(
            username="legacy-tombstone-other", email="legacy-tombstone@example.com")
        other_collection = services.personal_collection(other)
        urls = (
            ("github.com", "/owner/repo/blob/Release/ReadMe.MD",
             "/owner/repo/blob/release/readme.md"),
            ("gist.github.com", "/owner/0123456789abcdef/raw/Release/ReadMe.MD",
             "/owner/0123456789abcdef/raw/release/readme.md"),
        )
        for host, path, legacy_path in urls:
            with self.subTest(host=host):
                raw_url = (
                    f"https://{host.upper()}{path}?ref=Release"
                    "&utm_source=legacy#Install")
                key = publication.link_key(raw_url)
                self.assertEqual(key, f"https://{host}{path}?ref=Release")
                models.History.objects.create(
                    collection=self.personal, user=self.user,
                    kind=models.History.Kind.REMOVED,
                    url=f"https://{host}{legacy_path}?ref=Release")

                self.assertTrue(publication.tombstoned(
                    self.personal.id, self.user.id, raw_url, key))
                self.assertFalse(publication.tombstoned(
                    self.personal.id, other.id, raw_url, key))
                self.assertFalse(publication.tombstoned(
                    other_collection.id, self.user.id, raw_url, key))

        self.assertEqual(models.History.objects.filter(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED).count(), len(urls))

    def test_tombstoned_without_keys_is_false(self):
        self.assertFalse(publication.tombstoned(self.personal.id, self.user.id))

    def test_prompt_entry_reuses_exact_legacy_16_character_key(self):
        text = "An unchanged legacy prompt."
        legacy_key = "b45378c3b041e795"
        self.assertEqual(publication.prompt_key(text), legacy_key)
        legacy_entry = models.Entry.objects.create(
            collection=self.personal, kind=models.Entry.Kind.PROMPT, key=legacy_key)

        self.assertEqual(
            publication.prompt_entry(self.personal, self.user, text), legacy_entry)
        self.assertEqual(models.Entry.objects.filter(
            collection=self.personal, kind=models.Entry.Kind.PROMPT).count(), 1)

    def test_removed_alias_blocks_canonical_and_alias_urls(self):
        alias = "https://short.example/item"
        target = "https://target.example/item"
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.ALIAS, url=alias, target_url=target)
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=alias)
        self.assertTrue(publication.tombstoned(
            self.personal.id, self.user.id, target))

        alias2 = "https://short.example/other"
        target2 = "https://target.example/other"
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.ALIAS, url=alias2, target_url=target2)
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=target2)
        self.assertTrue(publication.tombstoned(
            self.personal.id, self.user.id, alias2))

    def test_conflicting_request_id_rejected(self):
        crid = str(uuid.uuid4())
        self.direct_import(items=[{"url": "https://example.com/a"}],
                           prompts=[], client_request_id=crid)
        with self.assertRaises(ValidationError):
            self.direct_import(items=[{"url": "https://example.com/b"}],
                               prompts=[], client_request_id=crid)

    def test_import_rejects_server_paths_outside_cache(self):
        secret = self.tmp / "secret_key"
        secret.write_bytes(b"private server bytes")
        for cache in ({"path": str(secret)}, str(secret), "../secret_key", r"C:\data\secret_key"):
            with self.subTest(cache=cache), self.assertRaises(ValidationError):
                self.direct_import(items=[{"url": "https://example.com/private",
                                           "error": "offline", "cache": cache}], prompts=[])
        self.assertEqual(secret.read_bytes(), b"private server bytes")
        self.assertFalse(models.Asset.objects.exists())

    def test_imported_media_never_overwrites_another_accounts_asset(self):
        retained = []
        with override_settings(DATA_DIR=str(self.tmp)):
            for i, color in enumerate(("red", "green", "blue")):
                user = models.User.objects.create_user(username=f"image-{i}", email=f"image-{i}@example.com")
                data = io.BytesIO()
                Image.new("RGB", (4, 4), color).save(data, "PNG")
                imports.import_items(user=user, collection_id=None, items=[{
                    "url": "https://example.com/shared-url",
                    "images": [{"b64": base64.b64encode(data.getvalue()).decode()}],
                }])
                asset = models.Asset.objects.get(collection=services.personal_collection(user))
                retained.append((self.tmp / asset.path, data.getvalue()))
                for path, expected in retained:
                    self.assertEqual(path.read_bytes(), expected)

    def test_cli_migration_ignores_shared_default_collection(self):
        shared = models.Collection.objects.create(name="Shared", kind="shared", owner=self.user)
        self.user.default_collection = shared
        self.user.save()
        self.run_import_command()
        self.assertFalse(models.Entry.objects.filter(collection=shared).exists())
        self.assertTrue(models.Entry.objects.filter(collection=self.personal).exists())

    def test_pending_lists_queued_job(self):
        self.run_import_command()
        out = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)):
            call_command("pending", "--user", "m1@example.com", stdout=out)
        self.assertIn("queued", out.getvalue())
        self.assertIn("https://example.com/pending", out.getvalue())

    def test_staged_legacy_record_imports_with_missing_media_recorded(self):
        # The browser-upload path: a legacy library.json staged under DATA_DIR
        # and drained through run_staged_import, with no import-cache present.
        staging_rel = "staging/legacy-library.json"
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_bytes(self.input_bytes)
            record = models.ImportRecord.objects.create(
                user=self.user, input_digest=uuid.uuid4().hex,
                manifest={"status": "pending", "format": "legacy",
                          "staging": staging_rel,
                          "collection_id": str(self.personal.id)})
            with mock.patch("clipshelf.acquisition.acquire",
                            side_effect=fake_acquire):
                result = imports.run_staged_import(record)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(record.manifest["result"]["counts"], result["counts"])
        # The cache/ page reference cannot be resolved (no import-cache dir):
        # reported honestly, not fatal.
        self.assertEqual(result["manifest"]["missing_media"], [CACHE_NAME])
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, kind="link",
            key="https://github.com/a/b").exists())
        self.assertEqual(models.Entry.objects.filter(
            collection=self.personal, kind="prompt").count(), 1)
        self.assertFalse(models.Entry.objects.filter(
            key="https://removed.example/x").exists())
        self.assertEqual(
            {c.origin for c in models.Contribution.objects.filter(
                entry__collection=self.personal)},
            {"legacy-import"})

    def test_staged_upload_reads_the_same_bytes_as_admission(self):
        # Admission parses the spooled bytes with json's BOM/UTF-16 sniffing;
        # the worker must use the same decoder, or a 201 upload dies in the
        # worker on its UTF-8 BOM (was read_text(encoding="utf-8")).
        staging_rel = "staging/bom-tiktok.json"
        payload = b"\xef\xbb\xbf" + json.dumps(
            [{"url": "https://example.com/bom", "desc": "clip"}]).encode()
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_bytes(payload)
            record = models.ImportRecord.objects.create(
                user=self.user, input_digest=uuid.uuid4().hex,
                manifest={"status": "pending", "format": "tiktok",
                          "staging": staging_rel,
                          "collection_id": str(self.personal.id)})
            with mock.patch("clipshelf.acquisition.acquire",
                            side_effect=fake_acquire):
                result = imports.run_staged_import(record)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(result["manifest"]["counts"]["links"], 1)

    def test_legacy_scalar_category_filters_on_api_without_assets(self):
        """A legacy item with a scalar `cat` and no cached page still lands in
        the right API category filter: regression for release e2233859, where
        the scalar was dropped and `?cat=ml` returned nothing."""
        url = "https://legacy.example/ml-note"
        result = self.direct_import(items=[{
            "url": url, "title": "ML note", "desc": "asset-free",
            "sources": ["dump.txt"], "tags": [], "found": "2024-01-01",
            "interpreted": "2024-02-01", "cat": "ml"}])
        self.assertEqual(result["manifest"]["counts"]["links"], 1)
        self.assertFalse(models.Job.objects.filter(url=url).exists(),
                         "asset-free item must not enqueue a job")
        entry = models.Entry.objects.get(collection=self.personal, key=url)

        client = Client()
        EmailAddress.objects.create(
            user=self.user, email=self.user.email, verified=True, primary=True)
        client.force_login(self.user)
        response = client.get("/api/entries", {"cat": "ml"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual([item["id"] for item in payload["entries"]], [str(entry.id)])
        self.assertEqual(payload["entries"][0]["title"], "ML note")
        self.assertEqual(payload["entries"][0]["cat"], ["ml"])

        contribution = models.Contribution.objects.get(
            entry=entry, origin="legacy-import")
        contribution.data["cat"] = 7
        contribution.save()
        self.assertEqual(services.serialize_entry(entry, self.user)["cat"], [])
        contribution.data["cat"] = ""
        contribution.save()
        self.assertEqual(services.serialize_entry(entry, self.user)["cat"], [])

    def test_import_calls_import_export_once_above_the_old_500_bound(self):
        """501 URL-only items must reach import_export in one call: the retired
        acquisition.IMPORT_MAX_ITEMS = 500 bound used to force a second batch
        call whose only purpose was to pass that deleted check."""
        items = [{"url": f"https://www.tiktok.com/@u/video/{i}"} for i in range(501)]
        calls = []
        real_import_export = acquisition.import_export

        def spy(items, directory, budget=None):
            calls.append(len(items))
            return real_import_export(items, directory, budget=budget)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire), \
                mock.patch("clipshelf.acquisition.import_export", side_effect=spy):
            result = imports.import_items(user=self.user, collection_id=None,
                                          items=items, prompts=[],
                                          cache_dir=str(self.cache_dir))
        self.assertEqual(calls, [501], "import_export must be called once")
        self.assertEqual(result["manifest"]["counts"]["links"], 501)

    def test_imported_category_labels_are_bounded(self):
        """Stored `cat` labels are replayed into later prompts, so an imported
        label must pass the same short-visible-label rule as model-named ones."""
        good = "Video generation"
        result = self.direct_import(
            items=[{"url": "https://example.com/a", "cat": "x" * 5000},
                   {"url": "https://example.com/b", "cat": good}],
            prompts=[{"text": "first prompt", "cat": "y" * 5000},
                     {"text": "second prompt", "cat": good}])
        self.assertTrue(result["created"])
        cats = {c for c in models.Contribution.objects.values_list(
                    "data__cat", flat=True)
                if isinstance(c, str) and c}
        self.assertEqual(cats, {good})
        long_findings = imports._legacy_findings(
            {"cat": "z" * 41, "desc": "d"}, "https://example.com/c")
        self.assertEqual(long_findings["categories"], [])
        self.assertTrue(any("not a short visible label" in w
                            for w in long_findings["warnings"]))
        self.assertEqual(imports._legacy_findings(
            {"cat": good}, "u")["categories"], [good])


class BackupRestoreTests(MigrationMixin, TransactionTestCase):
    def test_backup_rejects_assets_destinations_before_creating_files(self):
        data_dir = self.tmp / "backup-data"
        assets = data_dir / "assets"
        assets.mkdir(parents=True)
        rejected = []
        with override_settings(DATA_DIR=str(data_dir)):
            for destination in (assets, assets / "nested"):
                with mock.patch.object(BackupCommand, "_snapshot") as snapshot:
                    try:
                        call_command("backup", "--output", str(destination),
                                     stdout=io.StringIO())
                    except CommandError:
                        rejected.append(True)
                    else:
                        rejected.append(False)
                    snapshot.assert_not_called()

        self.assertEqual(rejected, [True, True])
        self.assertEqual(list(assets.iterdir()), [])
        self.assertFalse((data_dir / "data.lock").exists())

    def test_backup_rejects_existing_empty_destination(self):
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "empty-backup"
        target.mkdir()
        with override_settings(DATA_DIR=str(data_dir)), \
                mock.patch.object(BackupCommand, "_snapshot") as snapshot:
            with self.assertRaises(CommandError):
                call_command("backup", "--output", str(target), stdout=io.StringIO())
            snapshot.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((data_dir / "data.lock").exists())

    def _backup_spying_secrets_writes(self, fail):
        """Run a real backup; record the SECRETS.json fd mode at each write, optionally crash."""
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "secret-backup"
        modes = []
        real_fdopen = os.fdopen

        def fdopen(fd, *args, **kwargs):
            fh = real_fdopen(fd, *args, **kwargs)
            real_write = fh.write

            def write(text):
                modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
                if fail:
                    raise OSError("simulated crash during secrets write")
                return real_write(text)

            fh.write = write
            return fh

        old_umask = os.umask(0o022)  # the common default that made the old file 0644
        try:
            with override_settings(DATA_DIR=str(data_dir), SECRET_KEY="backup-secret-key",
                                   EMAIL_HOST_PASSWORD="smtp-password"), \
                    mock.patch("os.fdopen", side_effect=fdopen):
                call_command("backup", "--output", str(target), stdout=io.StringIO())
        finally:
            os.umask(old_umask)
        return target / "SECRETS.json", modes

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_backup_secrets_are_private_at_first_write(self):
        secrets_path, modes = self._backup_spying_secrets_writes(fail=False)
        self.assertEqual(modes, [0o600], "SECRETS.json was readable by others while written")
        self.assertEqual(stat.S_IMODE(secrets_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(secrets_path.read_text(encoding="utf-8")),
                         {"SECRET_KEY": "backup-secret-key",
                          "EMAIL_HOST_PASSWORD": "smtp-password"})

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_backup_secrets_stay_private_after_failed_write(self):
        with self.assertRaisesMessage(OSError, "simulated crash"):
            self._backup_spying_secrets_writes(fail=True)
        secrets_path = self.tmp / "secret-backup" / "SECRETS.json"
        self.assertEqual(stat.S_IMODE(secrets_path.stat().st_mode), 0o600,
                         "a crashed backup left SECRETS.json readable by others")
        self.assertNotIn("smtp-password", secrets_path.read_text(encoding="utf-8"))

    def test_backup_manifest_records_exact_config_and_database(self):
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "manifest-backup"
        config = {
            "DEFAULT_FROM_EMAIL": "backup@example.com",
            "EMAIL_HOST": "smtp.backup.example",
            "EMAIL_PORT": 587,
            "EMAIL_USE_TLS": True,
            "EMAIL_USE_SSL": False,
            "EMAIL_HOST_USER": "mailer",
            "ALLOWED_HOSTS": ["backup.example"],
            "CSRF_TRUSTED_ORIGINS": ["https://backup.example"],
        }
        with override_settings(DATA_DIR=str(data_dir), **config):
            call_command("backup", "--output", str(target), stdout=io.StringIO())
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config"],
                         {key: config[key] for key in CONFIG_KEYS})
        expected_name = Path(str(settings.DATABASES["default"]["NAME"])).name
        self.assertEqual(
            manifest["database"],
            {"engine": "sqlite", "path_name": expected_name,
             "sqlite_version": sqlite3.sqlite_version})

    def test_backup_restore_roundtrip_restores_database_file(self):
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "roundtrip-backup"
        fresh = self.tmp / "fresh-data"
        fresh_db = fresh / "db.sqlite3"
        with override_settings(DATA_DIR=str(data_dir)):
            call_command("backup", "--output", str(target), stdout=io.StringIO())
        with override_settings(DATA_DIR=str(fresh),
                               DATABASES={"default": {
                                   "ENGINE": "django.db.backends.sqlite3",
                                   "NAME": str(fresh_db)}}):
            call_command("restore", "--input", str(target), stdout=io.StringIO())
        con = sqlite3.connect(str(fresh_db))
        try:
            rows = con.execute(
                "SELECT count(*) FROM clipshelf_serversettings").fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(rows, 1)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_backup_directory_and_snapshot_are_private_under_default_umask(self):
        """Under umask 0022 the backup directory is 0700 and db.sqlite3 0600:
        a world-readable snapshot leaks session keys and stored API keys."""
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "private-backup"
        old_umask = os.umask(0o022)
        try:
            with override_settings(DATA_DIR=str(data_dir), SECRET_KEY="backup-secret-key"):
                call_command("backup", "--output", str(target), stdout=io.StringIO())
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700,
                         "the backup directory was readable by others")
        self.assertEqual(stat.S_IMODE((target / "db.sqlite3").stat().st_mode), 0o600,
                         "the snapshot database was readable by others")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_restore_publishes_private_modes_under_private_umask(self):
        """Restoring a public-mode (pre-0.11) backup under umask 077 must not
        republish the backup's modes: the restored database holds session
        keys, password hashes and API keys."""
        self.run_import_command()
        backup_dir = self.tmp / "public-bk"
        with override_settings(DATA_DIR=str(self.tmp)):
            call_command("backup", "--output", str(backup_dir), stdout=io.StringIO())
        os.chmod(backup_dir, 0o755)  # simulate a backup made under umask 0022
        os.chmod(backup_dir / "db.sqlite3", 0o644)
        for path in (backup_dir / "assets").rglob("*"):
            os.chmod(path, 0o755 if path.is_dir() else 0o644)

        fresh = self.tmp / "fresh-modes"
        fresh.mkdir()
        fresh_db = fresh / "restored.db"
        old_umask = os.umask(0o077)
        try:
            with override_settings(
                    DATA_DIR=str(fresh),
                    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                                           "NAME": str(fresh_db)}}):
                call_command("restore", "--input", str(backup_dir), stdout=io.StringIO())
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(fresh_db.stat().st_mode), 0o600,
                         "the restored database was readable by others")
        for path in (fresh / "assets").rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected,
                             f"restored {path.name} kept the backup's mode")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_imported_legacy_media_publishes_private_modes(self):
        """A 0644 legacy cache page published as an asset lands 0600 under the
        pinned private umask: copy2 used to copy the cache file's mode."""
        cache_page = self.cache_dir / Path(CACHE_NAME).name
        os.chmod(cache_page, 0o644)  # the runner pins umask 077; force a public page
        old_umask = os.umask(0o077)
        try:
            self.run_import_command()
        finally:
            os.umask(old_umask)
        asset = models.Asset.objects.get()
        self.assertEqual(stat.S_IMODE((self.tmp / asset.path).stat().st_mode), 0o600,
                         "published legacy media kept the cache file's mode")

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_cli_main_pins_private_umask(self):
        """cli.main sets umask 077 before anything else, so the data directory,
        WAL/SHM sidecars and backups are private whatever umask the caller had."""
        old_umask = os.umask(0o022)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["check"])
            self.assertEqual(os.umask(old_umask), 0o077,
                             "cli.main must pin umask 077 before creating files")
        finally:
            os.umask(old_umask)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_new_data_dir_is_private_under_default_umask(self):
        """End to end: in a subprocess with umask 0022, `clipshelf.py check`
        on a fresh CLIPSHELF_DATA_DIR leaves the directory 0700. settings.py
        creates DATA_DIR during django.setup(), so this fails if the umask
        call in cli.main moves below django.setup() (the in-process umask
        test above cannot see that ordering)."""
        data_dir = self.tmp / "fresh-data"
        env = dict(os.environ, CLIPSHELF_DATA_DIR=str(data_dir))
        proc = subprocess.run(
            [sys.executable, "clipshelf.py", "check"],
            cwd=Path(__file__).resolve().parent, env=env,
            preexec_fn=lambda: os.umask(0o022),
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700,
                         "a fresh data directory was readable by others")

    def test_concurrent_backups_publish_one_complete_generation(self):
        data_dir = self.tmp / "backup-data"
        data_dir.mkdir()
        target = self.tmp / "shared-backup"
        barrier = threading.Barrier(2)
        generation_lock = threading.Lock()
        generations = iter(("first", "second"))
        snapshot_calls = []

        def snapshot(_command, _db_path, destination):
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            with generation_lock:
                generation = next(generations)
                snapshot_calls.append(generation)
            (destination / f"{generation}.txt").write_text(generation, encoding="utf-8")

        def run_backup():
            command = BackupCommand(stdout=io.StringIO())
            try:
                command.handle(output=str(target), wait=5)
            except CommandError as exc:
                return exc
            return None

        with override_settings(DATA_DIR=str(data_dir)), \
                mock.patch("clipshelf.management.commands.backup.data_lock",
                           side_effect=lambda **kwargs: contextlib.nullcontext()), \
                mock.patch.object(BackupCommand, "_snapshot", new=snapshot):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: run_backup(), range(2)))

        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, CommandError) for result in results), 1)
        self.assertEqual(len(snapshot_calls), 1)
        published = list(target.iterdir())
        self.assertEqual([path.name for path in published], [f"{snapshot_calls[0]}.txt"])

    def test_backup_restore_roundtrip_isolated(self):
        self.run_import_command()
        instance_id = str(services.get_settings().instance_id)
        asset_rel = models.Asset.objects.get().path
        backup_dir = self.tmp / "bk"
        out = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)):
            call_command("backup", "--output", str(backup_dir), stdout=out)
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["instance_id"], instance_id)
        self.assertTrue((backup_dir / "db.sqlite3").is_file())
        self.assertTrue((backup_dir / "SECRETS.json").is_file())
        backed_assets = [p for p in (backup_dir / "assets").rglob("*") if p.is_file()]
        self.assertTrue(backed_assets, "media missing from backup")

        # live overwrite refused: a populated file-backed target (the runner's
        # own DB is a URI, which the non-file guard would reject first)
        live = self.tmp / "live"
        live.mkdir()
        live_db = live / "live.db"
        shutil.copy2(backup_dir / "db.sqlite3", live_db)
        with override_settings(
                DATA_DIR=str(live),
                DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                                       "NAME": str(live_db)}}):
            with self.assertRaisesMessage(CommandError, "refusing live overwrite"):
                call_command("restore", "--input", str(backup_dir))
        self.assertEqual(sorted(p.name for p in live.iterdir()), ["live.db"],
                         "refusal must precede staging")

        # isolated restore into an empty data directory
        fresh = self.tmp / "fresh"
        fresh.mkdir(parents=True)
        fresh_db = fresh / "restored.db"
        with override_settings(
                DATA_DIR=str(fresh),
                DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                                       "NAME": str(fresh_db)}}):
            call_command("restore", "--input", str(backup_dir))
            con = sqlite3.connect(str(fresh_db))
            try:
                table = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE '%serversettings'").fetchone()
                self.assertIsNotNone(table, "settings singleton missing after restore")
                restored_id = con.execute(
                    f"SELECT instance_id FROM {table[0]} LIMIT 1").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(str(uuid.UUID(str(restored_id))), instance_id,
                             "identity lost in restore")
            self.assertTrue((fresh / asset_rel).is_file(), "asset not restored")

    def test_restore_rejects_uri_database_without_littering_cwd(self):
        """Root cause of the zero-byte 'file' exhaust that dirtied every gate
        run: under the Django test runner DATABASES NAME is sqlite's
        shared-cache URI (file:memorydb_default?...), not a path. Restore used
        to treat it as a file, staged to garbage paths, and SQLite created a
        zero-byte 'file' in the working directory."""
        name = str(connection.settings_dict["NAME"])
        self.assertTrue(
            name == ":memory:" or name.startswith("file:"),
            f"precondition lost: test DB is file-backed ({name!r}); rewrite "
            "this test against the runner's real non-file name")
        backup_dir = self.tmp / "bk"
        backup_dir.mkdir()
        con = sqlite3.connect(str(backup_dir / "db.sqlite3"))
        try:
            con.execute("CREATE TABLE marker (x)")
            con.commit()
        finally:
            con.close()
        (backup_dir / "manifest.json").write_text("{}", encoding="utf-8")

        before = sorted(os.listdir(os.getcwd()))
        with self.assertRaises(CommandError) as ctx:
            call_command("restore", "--input", str(backup_dir))
        self.assertIn("file-backed", str(ctx.exception))
        self.assertEqual(sorted(os.listdir(os.getcwd())), before,
                         "restore littered the working directory")


def _interpreted_item(url):
    return {"url": url, "title": url, "desc": "legacy findings",
            "interpreted": "2024-02-01", "cat": "ml", "install": "pip install x",
            "cache": CACHE_NAME}


CLEAN_SCREEN = {"danger": 0.1, "related": {}}
WITHHELD_SCREEN = {"danger": 2.5, "related": {}}


class ImportScreeningTests(MigrationMixin, TransactionTestCase):
    """Import screening pre-flight: the judgment call runs outside the import
    transaction (the SQLite write lock is never held across it) and a flagged
    legacy item blocks only its own job, not the whole import."""

    def screened_import(self, items, screens):
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire",
                           side_effect=fake_acquire), \
                mock.patch.object(judgment, "available", return_value=True), \
                mock.patch.object(judgment, "screen_findings", side_effect=screens):
            return imports.import_items(user=self.user, collection_id=None,
                                       items=items, prompts=[],
                                       cache_dir=str(self.cache_dir))

    def test_screening_runs_outside_import_transaction(self):
        probes = []

        def probe(state, api_key=None):
            probes.append(connection.in_atomic_block)
            return dict(CLEAN_SCREEN)

        self.screened_import([_interpreted_item("https://github.com/a/b")], probe)
        job = models.Job.objects.get(url="https://github.com/a/b")
        self.assertEqual(job.state, "done")
        self.assertEqual(probes, [False],
                         "screening ran inside the import transaction: the "
                         "SQLite write lock was held across the network call")

    def test_flagged_item_blocks_only_its_job(self):
        result = self.screened_import(
            [_interpreted_item("https://github.com/a/b"),
             _interpreted_item("https://flagged.example/x")],
            [dict(CLEAN_SCREEN), dict(WITHHELD_SCREEN)])
        self.assertTrue(result["created"])
        self.assertEqual(result["counts"]["jobs"], 2)

        clean = models.Job.objects.get(url="https://github.com/a/b")
        self.assertEqual(clean.state, "done")
        self.assertEqual(clean.interpretation, "complete")
        self.assertFalse(clean.guardrail)
        self.assertEqual(clean.findings["categories"], ["ml"])

        flagged = models.Job.objects.get(url="https://flagged.example/x")
        self.assertEqual(flagged.state, "blocked")
        self.assertEqual(flagged.interpretation, "blocked")
        self.assertTrue(flagged.error.startswith("findings withheld:"))
        self.assertTrue(flagged.guardrail)
        self.assertEqual(flagged.findings, {}, "flagged findings must not be stored")
        self.assertTrue(models.Contribution.objects.filter(
            entry__key="https://flagged.example/x", user=self.user).exists(),
            "flagged item must not be dropped from the import")
        # The whole import committed: the flagged job did not roll it back.
        self.assertTrue(models.ImportRecord.objects.filter(user=self.user).exists())


class DeferredFetchTests(MigrationMixin, TestCase):
    def test_url_only_items_fetch_in_the_job_loop(self):
        note, done, cached = ("https://example.com/note", "https://example.com/done",
                              "https://example.com/cached")
        fetched = []

        def fake_acquire(url, directory):
            fetched.append(url)
            source = acquisition.new_source(url)
            source["acquisition"] = "partial"
            return source

        items = [{"url": note, "title": "Preview", "desc": f"my note {note}"},
                 {"url": done, "interpreted": "2024-02-01"},
                 {"url": cached, "cache": CACHE_NAME}]
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire):
            imports.import_items(user=self.user, collection_id=None, items=items,
                                cache_dir=str(self.cache_dir))
            # Interpreted and page-cached legacy entries still fetch inline.
            self.assertEqual(sorted(fetched), [cached, done])
            job = models.Job.objects.get(url=note)
            self.assertEqual((job.state, job.acquisition), ("queued", "pending"))
            self.assertTrue(worker._acquire_phase(job))
        self.assertEqual(fetched[-1], note)
        job.refresh_from_db()
        self.assertEqual(job.acquisition, "partial")
        # The export's preview title and note survive a page without them.
        self.assertEqual((job.source["title"], job.source["desc"]),
                         ("Preview", f"my note {note}"))

    def test_unresolved_legacy_media_fields_are_dropped(self):
        # v0.10.0 dropped unresolved references entirely; a raw leftover sent
        # the job loop into an inline fetch or a blocked state instead.
        a, b, c = ("https://example.com/a", "https://example.com/b",
                   "https://example.com/c")
        fetched = []
        items = [{"url": a, "cache": "cache/gone.html"},
                 {"url": b, "images": ["cache/gone.jpg"]},
                 {"url": c, "video": "cache/gone.mp4"}]
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire",
                           side_effect=lambda url, directory: fetched.append(url)):
            imports.import_items(user=self.user, collection_id=None, items=items,
                                 cache_dir=str(self.cache_dir))
            self.assertEqual(fetched, [],
                             "unresolved references must not trigger a fetch")
            for url in (a, b, c):
                job = models.Job.objects.get(url=url)
                self.assertEqual((job.state, job.acquisition),
                                 ("queued", "pending"))


class LegacyBindingBackfillTests(TransactionTestCase):
    """The 0004 bind_legacy_ids backfill: every accepted legacy request id
    must be bound to its ImportRecord before the retired client_request_id
    column is dropped (the migration is irreversible). The test database
    migrates from empty, so without replaying the upgrade the backfill runs
    over zero rows and a broken loop or a dropped RunPython ships undetected.
    """

    def test_0004_backfills_bindings_from_column_and_manifest(self):
        # Simulate a pre-upgrade server. The migration cannot be reversed, so
        # undo only its schema footprint by hand (the three statements mirror
        # 0003's schema exactly) and mark it unapplied: the forward run below
        # then re-executes the real migration, RunPython included.
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE clipshelf_importrequest")
            cursor.execute(
                "ALTER TABLE clipshelf_importrecord "
                "ADD COLUMN client_request_id uuid NULL")
            cursor.execute(
                "CREATE UNIQUE INDEX clipshelf_import_user_request_unique "
                "ON clipshelf_importrecord (user_id, client_request_id)")
        MigrationRecorder(connection).record_unapplied(
            "clipshelf", "0004_import_request_identity")

        executor = MigrationExecutor(connection)
        legacy = executor.loader.project_state(
            [("clipshelf", "0003_import_client_request_id")]).apps
        LegacyRecord = legacy.get_model("clipshelf", "ImportRecord")
        first = models.User.objects.create_user(username="mig1",
                                                email="mig1@example.com")
        second = models.User.objects.create_user(username="mig2",
                                                 email="mig2@example.com")

        def record(user_id, manifest, client_request_id=None):
            return LegacyRecord.objects.create(
                user_id=user_id, input_digest=uuid.uuid4().hex,
                manifest=manifest, client_request_id=client_request_id)

        col_id, man_id, both_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        collide_id, junk_id = uuid.uuid4(), uuid.uuid4()
        r_col = record(first.id, {}, col_id)  # id in the column only
        r_man = record(first.id, {"client_request_id": str(man_id)})
        r_both = record(first.id, {"client_request_id": str(both_id)}, both_id)
        r_early = record(first.id, {}, collide_id)
        # Hand-edited manifest colliding with an earlier record's column id.
        r_late = record(first.id, {"client_request_id": str(collide_id)})
        r_junk = record(first.id, {"client_request_id": "not-a-uuid"}, junk_id)
        r_second = record(second.id, {}, col_id)  # same id, different user
        # auto_now_add stamps near-simultaneous creates with identical clock
        # reads; pin the collision pair so "earliest record wins" is exact.
        LegacyRecord.objects.filter(id=r_late.id).update(
            created_at=r_early.created_at + timedelta(seconds=1))

        executor.migrate([("clipshelf", "0004_import_request_identity")])

        bindings = {(b.user_id, b.request_id): b.record_id
                    for b in models.ImportRequest.objects.all()}
        self.assertEqual(bindings, {
            (first.id, col_id): r_col.id,
            (first.id, man_id): r_man.id,
            (first.id, both_id): r_both.id,
            (first.id, collide_id): r_early.id,  # earliest record wins
            (first.id, junk_id): r_junk.id,  # manifest junk skipped
            (second.id, col_id): r_second.id,  # request ids are per user
        })

    def tearDown(self):
        # Restore the latest migration state whatever happened above (assert
        # failures included), so no other test ever sees the old schema.
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
