"""Restore publication regressions: crash-safe staging, atomic generation
cutover, in-lock recheck, and preserved (never deleted, never merged)
pre-existing assets. Uses a real temporary filesystem (Django test runner
discovers this file at the project root)."""
import contextlib
import io
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from clipshelf.management.commands import restore as restore_cmd


class RestoreMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clipshelf-restore-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.backup = self.tmp / "backup"
        (self.backup / "assets" / "abc").mkdir(parents=True)
        (self.backup / "assets" / "abc" / "0-page.html").write_text("page", encoding="utf-8")
        (self.backup / "assets" / "xyz.bin").write_text("blob", encoding="utf-8")
        con = sqlite3.connect(str(self.backup / "db.sqlite3"))
        try:
            for table in ("django_migrations", "clipshelf_user", "clipshelf_serversettings"):
                con.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY)')
            con.execute("INSERT INTO clipshelf_serversettings (id) VALUES ('test-instance')")
            con.commit()
        finally:
            con.close()
        (self.backup / "manifest.json").write_text(json.dumps(
            {"created_at": "2026-01-01T00:00:00+00:00", "instance_id": "test-instance",
             "counts": {"assets": 2}}), encoding="utf-8")
        self.fresh = self.tmp / "fresh"
        self.fresh.mkdir()
        self.fresh_db = self.fresh / "restored.db"
        override = override_settings(
            DATA_DIR=str(self.fresh),
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                                   "NAME": str(self.fresh_db)}})
        override.enable()
        self.addCleanup(override.disable)

    def restore(self):
        call_command(restore_cmd.Command(), "--input", str(self.backup))

    def db_tables(self):
        con = sqlite3.connect(str(self.fresh_db))
        try:
            return con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        finally:
            con.close()

    def page(self, root="assets"):
        return (self.fresh / root / "abc" / "0-page.html").read_text(encoding="utf-8")


class RestorePublicationTests(RestoreMixin, SimpleTestCase):
    def test_happy_path_publishes_assets_then_db_without_leftovers(self):
        self.assertFalse(self.fresh_db.exists())
        self.restore()
        self.assertGreater(self.db_tables(), 0)
        self.assertEqual(self.page(), "page")
        leftovers = sorted(self.fresh.glob("restore-staging-*")) + \
            sorted(self.fresh.glob(self.fresh_db.name + ".restore-*"))
        self.assertEqual(leftovers, [], "staged bytes must be renamed, not copied")

    def test_empty_backup_database_is_rejected_without_publication(self):
        (self.backup / "db.sqlite3").write_bytes(b"")
        previous = self.fresh / "assets"
        previous.mkdir()
        (previous / "keep.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "required table"):
            self.restore()

        self.assertFalse(self.fresh_db.exists())
        self.assertEqual((previous / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_damaged_backup_database_is_rejected_without_publication(self):
        source = self.backup / "db.sqlite3"
        con = sqlite3.connect(str(source))
        try:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            root_page = con.execute(
                "SELECT rootpage FROM sqlite_master "
                "WHERE name='clipshelf_serversettings'").fetchone()[0]
        finally:
            con.close()
        with source.open("r+b") as database:
            database.seek((root_page - 1) * page_size)
            database.write(b"\x00")
        previous = self.fresh / "assets"
        previous.mkdir()
        (previous / "keep.txt").write_text("keep", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "db.sqlite3"):
            self.restore()

        self.assertFalse(self.fresh_db.exists())
        self.assertEqual((previous / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_staging_failure_leaves_target_untouched_and_retry_succeeds(self):
        with mock.patch.object(restore_cmd.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertRaisesMessage(CommandError, "staging failed"):
                self.restore()
        self.assertEqual(self.db_tables(), 0)
        self.assertFalse((self.fresh / "assets").exists())
        self.restore()  # crash-visible recovery: a plain retry publishes
        self.assertGreater(self.db_tables(), 0)
        self.assertEqual(self.page(), "page")

    def test_crash_before_db_swap_keeps_assets_complete_and_retry_cuts_over(self):
        with mock.patch.object(restore_cmd.os, "replace", side_effect=OSError("crash")):
            with self.assertRaisesMessage(CommandError, "database swap"):
                self.restore()
        self.assertEqual(self.db_tables(), 0, "no database may appear before the commit")
        self.assertEqual(self.page(), "page", "published assets stay complete")
        # Retry: the crashed attempt's assets are renamed aside intact, never
        # merged or deleted, and the new backup content becomes canonical.
        (self.backup / "assets" / "abc" / "0-page.html").write_text("page-two", encoding="utf-8")
        self.restore()
        self.assertGreater(self.db_tables(), 0)
        self.assertEqual(self.page(), "page-two")
        gens = sorted(self.fresh.glob("assets.gen-*"))
        self.assertEqual(len(gens), 1, "exactly one preserved generation")
        self.assertEqual((gens[0] / "abc" / "0-page.html").read_text(encoding="utf-8"), "page")

    def test_guard_is_rechecked_under_the_exclusive_lock(self):
        real_lock = restore_cmd.data_lock

        @contextlib.contextmanager
        def populates_target(*args, **kwargs):
            with real_lock(*args, **kwargs):
                con = sqlite3.connect(str(self.fresh_db))
                try:
                    con.execute("CREATE TABLE live_now (id INTEGER)")
                    con.commit()
                finally:
                    con.close()
                yield

        with mock.patch.object(restore_cmd, "data_lock", populates_target):
            with self.assertRaisesMessage(CommandError, "refusing live overwrite"):
                self.restore()
        self.assertFalse((self.fresh / "assets").exists(), "refusal must precede cutover")

    def test_unreadable_nonempty_target_db_is_refused_and_preserved(self):
        corrupt = b"SQLite format 3\x00" + b"\xde\xad" * 600
        self.fresh_db.write_bytes(corrupt)
        with self.assertRaisesMessage(CommandError, "left untouched for recovery"):
            self.restore()
        self.assertEqual(self.fresh_db.read_bytes(), corrupt)
        self.assertFalse((self.fresh / "assets").exists(), "refusal must precede cutover")

    def test_zero_byte_target_db_is_debris_and_is_replaced(self):
        self.fresh_db.write_bytes(b"")
        self.restore()
        self.assertGreater(self.db_tables(), 0)

    def test_backup_without_assets_preserves_existing_assets_unmerged(self):
        (self.backup / "assets").rename(self.tmp / "assets-spare")
        (self.fresh / "assets").mkdir()
        (self.fresh / "assets" / "old.txt").write_text("old", encoding="utf-8")
        self.restore()
        self.assertGreater(self.db_tables(), 0)
        self.assertFalse((self.fresh / "assets").exists(), "no assets published, none merged")
        gens = sorted(self.fresh.glob("assets.gen-*"))
        self.assertEqual(len(gens), 1)
        self.assertTrue((gens[0] / "old.txt").is_file(), "previous assets preserved intact")

    def test_malformed_backup_is_refused_before_anything_is_published(self):
        (self.backup / "manifest.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesMessage(CommandError, "manifest.json"):
            self.restore()
        self.assertEqual(self.db_tables(), 0)
        self.assertFalse((self.fresh / "assets").exists())

    def test_manifest_must_be_a_json_object_before_publication(self):
        (self.backup / "manifest.json").write_text("[]", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "manifest.json"):
            self.restore()

        self.assertFalse(self.fresh_db.exists())
        self.assertFalse((self.fresh / "assets").exists())

    def test_malformed_secrets_are_rejected_before_publication_without_printing_values(self):
        sensitive_value = "restore-test-value"
        cases = (
            ("invalid JSON", "{broken"),
            ("non-object", json.dumps([sensitive_value])),
            ("non-string value", json.dumps({"SECRET_KEY": [sensitive_value]})),
        )
        output = io.StringIO()
        for name, content in cases:
            with self.subTest(name=name):
                (self.backup / "SECRETS.json").write_text(content, encoding="utf-8")
                with self.assertRaisesMessage(CommandError, "SECRETS.json") as raised:
                    call_command(
                        restore_cmd.Command(), "--input", str(self.backup), stdout=output)

                self.assertNotIn(sensitive_value, str(raised.exception))
                self.assertNotIn(sensitive_value, output.getvalue())
                self.assertFalse(self.fresh_db.exists())
                self.assertFalse((self.fresh / "assets").exists())
