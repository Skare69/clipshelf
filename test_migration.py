"""Legacy migration regressions: manifest counts/digests, idempotency, scope,
settings recording, and backup/restore roundtrip."""
import base64
import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from PIL import Image

from clipshelf import models, services, worker

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


def fake_import_export(batch, directory):
    """Mirror the real flow without network: copy images/video specs handed to
    us, ignore unknown fields (page caches are appended by the worker itself),
    and report complete acquisition."""
    out = []
    for item in batch:
        assets = []
        for spec in (item.get("images") or []):
            if isinstance(spec, dict) and spec.get("path"):
                dest = Path(directory) / Path(spec["path"]).name
                shutil.copy2(spec["path"], dest)
                assets.append({"path": str(dest), "kind": "image",
                               "content_type": "application/octet-stream",
                               "position": len(assets)})
        video = item.get("video")
        if isinstance(video, dict) and video.get("path"):
            dest = Path(directory) / Path(video["path"]).name
            shutil.copy2(video["path"], dest)
            assets.append({"path": str(dest), "kind": "video",
                           "content_type": "video/mp4", "position": len(assets)})
        out.append({"url": item["url"], "original_url": item["url"], "title": "",
                    "desc": "", "text": "", "links": [], "assets": assets,
                    "acquisition": "complete", "warnings": [], "metadata": {}})
    return out


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
                mock.patch("clipshelf.acquisition.import_export",
                           side_effect=fake_import_export):
            call_command("import_library", *args, stdout=out)
        return out.getvalue()

    def direct_import(self, items=None, prompts=None, dry_run=False, **kw):
        """Direct worker.import_items call against this fixture's library."""
        items = items if items is not None else [
            {"url": u, **e} for u, e in self.library["links"].items()]
        prompts = prompts if prompts is not None else list(self.library["prompts"].values())
        kw.setdefault("cache_dir", str(self.cache_dir))
        kw.setdefault("dry_run", dry_run)
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.import_export",
                           side_effect=fake_import_export):
            return worker.import_items(user=self.user, collection_id=None,
                                       items=items, prompts=prompts,
                                       seen=self.library["seen"],
                                       removed=self.library["removed"], **kw)


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
                worker.import_items(user=user, collection_id=None, items=[{
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
            with mock.patch("clipshelf.acquisition.import_export",
                            side_effect=fake_import_export):
                result = worker.run_staged_import(record)
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


class BackupRestoreTests(MigrationMixin, TransactionTestCase):
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

        # live overwrite refused: the current database already has tables
        with override_settings(DATA_DIR=str(self.tmp)):
            with self.assertRaises(CommandError):
                call_command("restore", "--input", str(backup_dir))

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
