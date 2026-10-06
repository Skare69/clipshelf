import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from clipshelf import models
from clipshelf.management.commands.ingest import MAX_TEXT_BYTES


class IngestCommandTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = models.User.objects.create_user(
            username="ingest", email="ingest@example.com")
        self.first = models.Collection.objects.create(
            name="First", kind="shared", owner=self.user)
        self.second = models.Collection.objects.create(
            name="Second", kind="shared", owner=self.user)

    def ingest(self, path, collection=None):
        output = io.StringIO()
        args = ["--user", self.user.email]
        if collection is not None:
            args.extend(("--collection", str(collection.id)))
        call_command("ingest", *args, str(path), stdout=output)
        return json.loads(output.getvalue())

    def test_idempotency_is_scoped_to_destination(self):
        dump = self.root / "dump.txt"
        dump.write_text("https://example.com/item\n", encoding="utf-8")

        first = self.ingest(dump, self.first)
        replay = self.ingest(dump, self.first)
        second = self.ingest(dump, self.second)

        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(second["created"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            set(models.Capture.objects.values_list("collection_id", flat=True)),
            {self.first.id, self.second.id},
        )

    def test_oversize_file_read_is_bounded(self):
        dump = self.root / "large.txt"
        dump.write_bytes(b"x" * (MAX_TEXT_BYTES + 10))
        real_open = Path.open
        read_sizes = []

        class ReadSpy:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self.stream.__exit__(*exc)

            def read(self, size=-1):
                read_sizes.append(size)
                return self.stream.read(size)

        def open_spy(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return ReadSpy(stream) if path == dump else stream

        with mock.patch.object(Path, "open", autospec=True, side_effect=open_spy):
            with self.assertRaises(CommandError):
                self.ingest(dump)

        self.assertEqual(read_sizes, [MAX_TEXT_BYTES + 1])

    def test_invalid_utf8_has_clear_command_error(self):
        dump = self.root / "invalid.txt"
        dump.write_bytes(b"\xff")

        with self.assertRaisesMessage(CommandError, "UTF-8"):
            self.ingest(dump)
