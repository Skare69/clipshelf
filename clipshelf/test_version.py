"""The footer version links to its GitHub release and notes a newer one."""
import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from clipshelf import network, update
from clipshelf.context_processors import version


class VersionLinkTests(SimpleTestCase):
    def test_tagged_build_links_its_release_page(self):
        with mock.patch.dict(os.environ, {"CLIPSHELF_VERSION": "0.12.1", "CLIPSHELF_UPDATE_CHECK": "0"}):
            ctx = version(None)
        self.assertEqual(ctx["CLIPSHELF_VERSION"], "0.12.1")
        self.assertEqual(ctx["CLIPSHELF_RELEASE_URL"],
                         "https://github.com/Skare69/clipshelf/releases/tag/v0.12.1")

    def test_dev_build_links_the_release_list(self):
        with mock.patch.dict(os.environ, {"CLIPSHELF_VERSION": "", "CLIPSHELF_UPDATE_CHECK": "0"}):
            ctx = version(None)
        self.assertEqual(ctx["CLIPSHELF_VERSION"], "dev")
        self.assertEqual(ctx["CLIPSHELF_RELEASE_URL"],
                         "https://github.com/Skare69/clipshelf/releases")


class _SyncThread:
    """Runs the background check inline so a test sees its result."""

    def __init__(self, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _release(tag, url=None):
    body = {"tag_name": tag,
            "html_url": url or f"https://github.com/Skare69/clipshelf/releases/tag/{tag}"}
    return {"url": update.LATEST_URL, "body": json.dumps(body).encode(),
            "content_type": "application/json"}


class IsNewerTests(SimpleTestCase):
    def test_compare(self):
        cases = [("0.13.0", "0.12.1", True), ("0.12.10", "0.12.9", True),
                 ("1", "0.99.99", True), ("0.12.1", "0.12.1", False),
                 ("0.12.1.0", "0.12.1", False), ("0.12.0", "0.12.1", False),
                 ("0.13.0-rc1", "0.12.1", False), ("0.13.0", "dev", False),
                 ("", "0.12.1", False), ("0.13.", "0.12.1", False)]
        for latest, current, want in cases:
            with self.subTest(latest=latest, current=current):
                self.assertIs(update.is_newer(latest, current), want)


@mock.patch.object(update.threading, "Thread", _SyncThread)
class AvailableUpdateTests(TestCase):
    def setUp(self):
        update.reset()
        env = mock.patch.dict(os.environ, {"CLIPSHELF_UPDATE_CHECK": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(update.reset)

    def test_newer_release_shows_after_the_check(self):
        with mock.patch.object(update.network, "fetch_public",
                               return_value=_release("v0.13.0")) as fetch:
            update.available_update("0.12.1")
            got = update.available_update("0.12.1")
            update.available_update("0.12.1")
        self.assertEqual(got, {"version": "0.13.0",
                               "url": "https://github.com/Skare69/clipshelf/releases/tag/v0.13.0"})
        self.assertEqual(fetch.call_count, 1)  # cached for FRESH_S

    def test_equal_or_older_release_shows_nothing(self):
        for tag in ("v0.12.1", "v0.11.0"):
            update.reset()
            with self.subTest(tag=tag), mock.patch.object(
                    update.network, "fetch_public", return_value=_release(tag)):
                update.available_update("0.12.1")
                self.assertIsNone(update.available_update("0.12.1"))

    def test_foreign_release_url_is_refused(self):
        with mock.patch.object(update.network, "fetch_public",
                               return_value=_release("v9.0.0", "https://evil.example/x")):
            update.available_update("0.12.1")
            self.assertIsNone(update.available_update("0.12.1"))

    def test_failed_check_shows_nothing_and_backs_off(self):
        with mock.patch.object(update.network, "fetch_public",
                               side_effect=network.NetworkError("HTTP 403")) as fetch, \
                self.assertLogs("clipshelf.update", "WARNING"):
            update.available_update("0.12.1")
            self.assertIsNone(update.available_update("0.12.1"))
        self.assertEqual(fetch.call_count, 1)  # inside the 60 s window

    def test_failed_recheck_keeps_the_last_good_answer(self):
        with mock.patch.object(update.network, "fetch_public",
                               return_value=_release("v0.13.0")):
            update.available_update("0.12.1")
        update._state["at"] -= update.FRESH_S
        with mock.patch.object(update.network, "fetch_public",
                               side_effect=network.NetworkError("down")), \
                self.assertLogs("clipshelf.update", "WARNING"):
            update.available_update("0.12.1")
            self.assertEqual(update.available_update("0.12.1")["version"], "0.13.0")

    def test_opt_out_skips_the_network(self):
        with mock.patch.dict(os.environ, {"CLIPSHELF_UPDATE_CHECK": "0"}), \
                mock.patch.object(update.network, "fetch_public") as fetch:
            self.assertIsNone(update.available_update("0.12.1"))
        fetch.assert_not_called()

    def test_notice_renders_in_the_footer(self):
        with mock.patch.object(update.network, "fetch_public",
                               return_value=_release("v0.13.0")), \
                mock.patch.dict(os.environ, {"CLIPSHELF_VERSION": "0.12.1"}):
            update.available_update("0.12.1")
            # an instance without accounts redirects to setup
            get_user_model().objects.create_user("u", "u@example.com", "pw-0123456789")
            html = self.client.get("/accounts/login/").content.decode()
        self.assertIn('<a class="upd" href="https://github.com/Skare69/clipshelf/'
                      'releases/tag/v0.13.0"', html)
        self.assertIn('href="https://github.com/Skare69/clipshelf/releases/tag/v0.12.1">'
                      'v0.12.1</a>', html)
