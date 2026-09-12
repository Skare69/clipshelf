"""Operator ingest of a text dump: same acceptance path as captures."""
import hashlib
import json
import uuid
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from clipshelf import models, services

MAX_TEXT_BYTES = 32768  # same boundary the capture API enforces


class Command(BaseCommand):
    help = ("Ingest a text dump of URLs for an explicit user/collection, "
            "through the same validated acceptance path as the API. Re-ingesting "
            "the identical file returns the existing receipt instead of duplicating.")

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--collection", default=None, help="writable collection UUID")
        parser.add_argument("file")

    def handle(self, *args, **options):
        user = models.User.objects.filter(email__iexact=options["user"].strip()).first()
        if user is None:
            raise CommandError(f"no account for {options['user']}")
        path = Path(options["file"])
        if not path.is_file():
            raise CommandError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise CommandError(f"file exceeds {MAX_TEXT_BYTES} UTF-8 bytes (capture limit)")

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        client_request_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"clipshelf-ingest:{user.id}:{digest}"))
        try:
            capture, created = services.accept_capture(
                user, client_request_id, text, options["collection"])
        except ValidationError as exc:
            raise CommandError(f"rejected: {exc.message if hasattr(exc, 'message') else exc}")
        payload = services.receipt(capture)
        payload["created"] = created
        self.stdout.write(self.style.SUCCESS(json.dumps(payload, indent=2, default=str)))
