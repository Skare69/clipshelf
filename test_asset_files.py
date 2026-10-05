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
from django.test import SimpleTestCase, TestCase, override_settings

from clipshelf import asset_files, models, publication, services


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

    def test_relative_data_dir_publish_then_prepare(self):
        original_cwd = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, original_cwd)
        staging = self.stage("0-page.html", b"relative data dir")
        source = {"assets": [{"path": str(staging / "0-page.html"), "kind": "page"}]}
        with override_settings(DATA_DIR="relative-data"):
            published = asset_files.publish(source, staging, move=True)
            prepared = asset_files.prepare(published)
        data_dir = self.tmp / "relative-data"
        destination = data_dir / prepared[0]["path"]
        self.assertEqual(
            prepared[0]["path"],
            Path(published[0]["path"]).relative_to(data_dir).as_posix(),
        )
        self.assertEqual(destination.read_bytes(), b"relative data dir")

    def test_escape_from_staging_and_missing_file_are_rejected(self):
        staging = self.stage("0-page.html", b"hi")
        outside = self.tmp / "outside.bin"
        outside.write_bytes(b"outside")
        escape = {"assets": [{"path": str(outside), "kind": "page"}]}
        with self.assertRaises(ValidationError):
            asset_files.publish(escape, staging, move=True)
        self.assertEqual(outside.read_bytes(), b"outside")
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

    def test_prepare_caps_at_500_assets(self):
        folder = self.tmp / "assets" / "bulk"
        folder.mkdir(parents=True)
        assets = []
        for i in range(502):
            target = folder / f"{i}-part.bin"
            target.write_bytes(b"x")
            assets.append({"path": str(target), "kind": "page"})
        prepared = asset_files.prepare(assets)
        self.assertEqual(len(prepared), asset_files.MAX_ASSETS)
        self.assertEqual(prepared[0]["path"], (folder / "0-part.bin").relative_to(self.tmp).as_posix())
        self.assertEqual(prepared[-1]["path"], (folder / "499-part.bin").relative_to(self.tmp).as_posix())

    def test_prepare_rejection_errors_name_the_assets_field(self):
        with self.assertRaises(ValidationError) as ctx:
            asset_files.prepare(["nope"])
        self.assertIn("assets", ctx.exception.message_dict)
        # suffix marker: the field error names the expected shape, not free text
        self.assertTrue(ctx.exception.message_dict["assets"][0].endswith("asset objects."))
        with self.assertRaises(ValidationError) as kind_ctx:
            asset_files.prepare([{"path": str(self.store(b"x")), "kind": "hologram"}])
        self.assertIn("assets", kind_ctx.exception.message_dict)
        self.assertTrue(kind_ctx.exception.message_dict["assets"][0].endswith("asset kind."))

    def test_prepare_truncates_content_type_to_255_chars(self):
        target = self.store(b"x")
        prepared = asset_files.prepare(
            [{"path": str(target), "kind": "page", "content_type": "t" * 300}]
        )
        self.assertEqual(prepared[0]["content_type"], "t" * 255)


class LinkKeyTests(SimpleTestCase):
    """The urlsplit fallback only runs for URLs lib.norm rejects (credentials,
    non-http schemes); explicit default ports collapse only there."""

    def test_fallback_drops_default_ports(self):
        self.assertEqual(publication.link_key("http://user:pass@Host:80/x"), "http://host/x")
        self.assertEqual(publication.link_key("https://user:pass@Host:443/a"), "https://host/a")

    def test_fallback_keeps_non_default_ports(self):
        self.assertEqual(
            publication.link_key("http://user:pass@host:8443/x"), "http://host:8443/x"
        )

    def test_fallback_defaults_empty_path_to_root(self):
        self.assertEqual(publication.link_key("ftp://host"), "ftp://host/")


class PromptKeyTests(SimpleTestCase):
    def test_prompt_key_is_legacy_sha1_prefix(self):
        self.assertEqual(
            publication.prompt_key("Hello World"),
            hashlib.sha1(b"hello world").hexdigest()[:16],
        )
        self.assertEqual(publication.prompt_key("  a   b "), publication.prompt_key("A B"))
        self.assertEqual(len(publication.prompt_key("x")), 16)


class PublishPromptTests(TestCase):
    def setUp(self):
        self.user = models.User.objects.create_user(username="pub1", email="pub1@example.com")
        self.collection = services.personal_collection(self.user)

    def test_publish_prompt_reports_published_vs_removed(self):
        self.assertTrue(publication.publish_prompt(
            collection=self.collection, user=self.user, text="first prompt",
            data={"text": "first prompt"}, origin="capture"))
        entry = models.Entry.objects.get(collection=self.collection, kind="prompt")
        self.assertEqual(entry.key, publication.prompt_digest("first prompt"))
        self.assertEqual(
            models.Contribution.objects.get(entry=entry, user=self.user).data["text"],
            "first prompt")
        models.History.objects.create(
            collection=self.collection, user=self.user,
            kind=models.History.Kind.REMOVED, url=publication.prompt_digest("removed"))
        self.assertFalse(publication.publish_prompt(
            collection=self.collection, user=self.user, text="removed",
            data={"text": "removed"}, origin="capture"))
        self.assertFalse(models.Entry.objects.filter(
            collection=self.collection, kind="prompt", key=publication.prompt_digest("removed")
        ).exists())
