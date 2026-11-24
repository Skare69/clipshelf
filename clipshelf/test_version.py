"""The footer version links to the GitHub release of the running build."""
import os
from unittest import mock

from django.test import SimpleTestCase

from clipshelf.context_processors import version


class VersionLinkTests(SimpleTestCase):
    def test_tagged_build_links_its_release_page(self):
        with mock.patch.dict(os.environ, {"CLIPSHELF_VERSION": "0.12.1"}):
            ctx = version(None)
        self.assertEqual(ctx["CLIPSHELF_VERSION"], "0.12.1")
        self.assertEqual(ctx["CLIPSHELF_RELEASE_URL"],
                         "https://github.com/Skare69/clipshelf/releases/tag/v0.12.1")

    def test_dev_build_links_the_release_list(self):
        with mock.patch.dict(os.environ, {"CLIPSHELF_VERSION": ""}):
            ctx = version(None)
        self.assertEqual(ctx["CLIPSHELF_VERSION"], "dev")
        self.assertEqual(ctx["CLIPSHELF_RELEASE_URL"],
                         "https://github.com/Skare69/clipshelf/releases")
