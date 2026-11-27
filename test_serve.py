"""clipshelf.py serve tuning: the request-body cap tracks the import ceiling.

Standalone self-check (`$PY test_serve.py`): idempotent under Django's own
test discovery, like test_worker.py.
"""
import os
from unittest import mock

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")
import django

django.setup()

from django.conf import settings
from django.test import SimpleTestCase

from clipshelf import cli


class ServeBodyCapTest(SimpleTestCase):
    def test_body_cap_tracks_import_ceiling(self):
        with mock.patch("waitress.serve") as serve:
            cli._serve("127.0.0.1", 8000)
        self.assertEqual(
            serve.call_args.kwargs["max_request_body_size"],
            settings.CLIPSHELF_MAX_IMPORT_BYTES + 1024 * 1024,
        )
        # The existing slow-admin-request budget stays in place.
        self.assertEqual(serve.call_args.kwargs["channel_timeout"], 900)
