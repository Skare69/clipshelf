"""Merge interpret-skill findings JSON into a user's Personal collection."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from clipshelf import models, worker

MAX_FINDINGS_BYTES = 32 * 1024 * 1024


class Command(BaseCommand):
    help = ("Merge findings (the interpretation skill's JSON output) for an "
            "explicit user. Legacy merge semantics: longer title/desc wins, "
            "tags and sources union, never executed.")

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("file")

    def handle(self, *args, **options):
        user = models.User.objects.filter(email__iexact=options["user"].strip()).first()
        if user is None:
            raise CommandError(f"no account for {options['user']}")
        path = Path(options["file"])
        if not path.is_file():
            raise CommandError(f"file not found: {path}")
        if path.stat().st_size > MAX_FINDINGS_BYTES:
            raise CommandError(f"file exceeds {MAX_FINDINGS_BYTES} bytes")
        payload = json.loads(path.read_text(encoding="utf-8"))
        findings = payload if isinstance(payload, list) else [payload]
        if not findings or not all(isinstance(f, dict) for f in findings):
            raise CommandError("findings file must be a JSON object or list of objects")
        stats = worker.merge_findings(user, findings)
        self.stdout.write(self.style.SUCCESS(
            f"merged: {stats['links']} new link(s), {stats['prompts']} new prompt(s), "
            f"{stats['updated']} updated for {user.email}"))
