"""Retained-file custody regressions: path resolution safety, publication
move/copy semantics, and store-prep validation. Uses a real temporary
filesystem (Django test runner discovers this file at the project root)."""
import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from clipshelf import asset_files


class CustodyMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clipshelf-custody-"))
        override = override_settings(DATA_DIR=str(self.tmp))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def stage(self, name, payload):
        staging = self.tmp / "staging" / uuid.uuid4().hex
        staging.mkdir(parents=True)
        (staging / name).write_bytes(payload)
        return staging


class ResolvePathTests(CustodyMixin, SimpleTestCase):
    def test_relative_stored_path_resolves_under_data_dir(self):
        (self.tmp / "assets" / "abc").mkdir(parents=True)
        target = self.tmp / "assets" / "abc" / "0-page.html"
        target.write_text("hi")
        self.assertEqual(asset_files.resolve_path("assets/abc/0-page.html"), target.resolve())

    def test_backslashes_are_normalized(self):
        self.assertEqual(
            asset_files.resolve_path("assets\\abc\\0-page.html"),
            asset_files.resolve_path("assets/abc/0-page.html"),
        )

    def test_absolute_path_inside_root_is_accepted(self):
        target = self.tmp / "assets" / "kept.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        self.assertEqual(asset_files.resolve_path(str(target)), target.resolve())

    def test_empty_or_non_string_paths_are_rejected(self):
        for bad in ("", "   ", None, 123, b"assets/x"):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                asset_files.resolve_path(bad)

    def test_traversal_escape_is_rejected(self):
        with self.assertRaises(ValidationError):
            asset_files.resolve_path("../outside.txt")
        with self.assertRaises(ValidationError):
            asset_files.resolve_path("assets/../../outside.txt")

    def test_windows_drive_and_unc_paths_rejected_on_posix(self):
        if os.name == "nt":
            self.skipTest("foreign-path rejection applies when running on POSIX")
        for bad in ("C:/Windows/evil.txt", "C:evil.txt", "\\\\server\\share\\x.txt",
                    "//server/share/x.txt"):
            with self.assertRaises(ValidationError, msg=bad):
                asset_files.resolve_path(bad)

    def test_symlink_escape_is_rejected(self):
        outside_dir = Path(tempfile.mkdtemp(prefix="clipshelf-outside-"))
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside = outside_dir / "outside.txt"
        outside.write_text("secret")
        link = self.tmp / "link.txt"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("symlink creation privilege unavailable on this platform")
        with self.assertRaises(ValidationError):
            asset_files.resolve_path("link.txt")


class PublishTests(CustodyMixin, SimpleTestCase):
    def test_move_consumes_staging_and_publishes_complete_file(self):
        staging = self.stage("0-page.html", b"<html>acquired</html>")
        source = {"assets": [{"path": str(staging / "0-page.html"), "kind": "page"}]}
        published = asset_files.publish(source, staging, move=True)
        dest = Path(published[0]["path"])
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"<html>acquired</html>")
        self.assertFalse((staging / "0-page.html").exists())

    def test_copy_keeps_source_reusable_across_entries(self):
        staging = self.stage("0-image.png", b"\x89PNG")
        staged = str(staging / "0-image.png")
        first = asset_files.publish({"assets": [{"path": staged, "kind": "image"}]},
                                    staging, move=False)
        second = asset_files.publish({"assets": [{"path": staged, "kind": "image"}]},
                                     staging, move=False)
        self.assertTrue((staging / "0-image.png").is_file())  # cache may feed multiple entries
        self.assertNotEqual(Path(first[0]["path"]), Path(second[0]["path"]))
        self.assertEqual(
            Path(first[0]["path"]).read_bytes(), (staging / "0-image.png").read_bytes()
        )

    def test_repeated_publication_never_overwrites_prior_bytes(self):
        staging = self.stage("0-page.html", b"v1")
        staged = str(staging / "0-page.html")
        first = asset_files.publish({"assets": [{"path": staged, "kind": "page"}]},
                                    staging, move=False)
        (staging / "0-page.html").write_bytes(b"v2")
        second = asset_files.publish({"assets": [{"path": staged, "kind": "page"}]},
                                     staging, move=False)
        self.assertEqual(Path(first[0]["path"]).read_bytes(), b"v1")
        self.assertEqual(Path(second[0]["path"]).read_bytes(), b"v2")

    def test_publish_sets_source_assets_and_preserves_fields(self):
        staging = self.stage("0-page.html", b"hi")
        asset = {"path": str(staging / "0-page.html"), "kind": "page",
                 "content_type": "text/html", "position": 3}
        source = {"assets": [asset]}
        published = asset_files.publish(source, staging, move=True)
        self.assertEqual(source["assets"], published)
        self.assertEqual(published[0]["kind"], "page")
        self.assertEqual(published[0]["position"], 3)
        self.assertNotEqual(published[0]["path"], str(staging / "0-page.html"))

    def test_escape_from_staging_and_missing_file_are_rejected(self):
        staging = self.stage("0-page.html", b"hi")
        escape = {"assets": [{"path": str(self.tmp / "outside.bin"), "kind": "page"}]}
        with self.assertRaises(ValidationError):
            asset_files.publish(escape, staging, move=True)
        missing = {"assets": [{"path": str(staging / "gone.bin"), "kind": "page"}]}
        with self.assertRaises(ValidationError):
            asset_files.publish(missing, staging, move=False)

    def test_empty_assets_publish_nothing(self):
        self.assertEqual(asset_files.publish({"assets": []}, self.tmp, move=True), [])
        self.assertEqual(asset_files.publish({}, self.tmp, move=False), [])


class PrepareTests(CustodyMixin, SimpleTestCase):
    def store(self, payload, name="0-page.html"):
        folder = self.tmp / "assets" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        (folder / name).write_bytes(payload)
        return folder / name

    def test_prepare_measures_and_returns_data_dir_relative_posix_paths(self):
        target = self.store(b"retained bytes")
        prepared = asset_files.prepare(
            [{"path": str(target), "kind": "page", "content_type": "text/html; charset=utf-8"}]
        )
        self.assertEqual(prepared[0]["path"], target.relative_to(self.tmp).as_posix())
        self.assertEqual(prepared[0]["size"], len(b"retained bytes"))
        self.assertEqual(
            prepared[0]["sha256"], hashlib.sha256(b"retained bytes").hexdigest()
        )
        self.assertEqual(prepared[0]["content_type"], "text/html; charset=utf-8")
        self.assertEqual(prepared[0]["position"], 0)
        self.assertNotIn("\\", prepared[0]["path"])

    def test_prepare_requires_existing_valid_assets(self):
        missing = self.tmp / "assets" / "nope" / "0-page.html"
        cases = [
            ("not-a-list", {}),
            ("bad-item", ["nope"]),
            ("no-path", [{"kind": "page"}]),
            ("missing-file", [{"path": str(missing), "kind": "page"}]),
            ("bad-kind", [{"path": str(self.store(b"x")), "kind": "hologram"}]),
            ("bad-position", [{"path": str(self.store(b"x")), "kind": "page",
                               "position": "soon"}]),
            ("escape", [{"path": "../outside.bin", "kind": "page"}]),
        ]
        for label, arg in cases:
            with self.subTest(label):
                with self.assertRaises(ValidationError):
                    asset_files.prepare(arg)

    def test_prepare_defaults_position_to_index(self):
        first = self.store(b"a")
        second = self.store(b"b")
        prepared = asset_files.prepare(
            [{"path": str(first), "kind": "page"}, {"path": str(second), "kind": "image",
                                                    "position": 7}]
        )
        self.assertEqual([p["position"] for p in prepared], [0, 7])
