"""Merge skill findings (SKILL.md schema) JSON into a user's Personal collection."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from clipshelf import imports
from clipshelf.management import user_for

MAX_FINDINGS_BYTES = 32 * 1024 * 1024


class Command(BaseCommand):
    help = ("Merge skill findings (the clipshelf-interpret SKILL.md JSON schema) "
            "for an explicit user. Legacy merge semantics: longer title/desc "
            "wins, tags and sources union, never executed.")

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("file")

    def handle(self, *args, **options):
        user = user_for(options["user"])
        path = Path(options["file"])
        if not path.is_file():
            raise CommandError(f"file not found: {path}")
        if path.stat().st_size > MAX_FINDINGS_BYTES:
            raise CommandError(f"file exceeds {MAX_FINDINGS_BYTES} bytes")
        payload = json.loads(path.read_text(encoding="utf-8"))
        skill_findings = payload if isinstance(payload, list) else [payload]
        if not skill_findings or not all(isinstance(f, dict) for f in skill_findings):
            raise CommandError("findings file must be a JSON object or list of objects")
        for finding in skill_findings:
            for field in ("links", "prompts"):
                values = finding.get(field, [])
                fields = (("url", "title") if field == "links"
                          else ("text", "title", "cat"))
                if (not isinstance(values, list)
                        or not all(isinstance(item, dict) for item in values)
                        or any(not isinstance(item.get(key, ""), str)
                               for item in values for key in fields)):
                    raise CommandError(
                        f"finding {field} must be a list of objects with string fields")
            for field in ("source", "summary"):
                if not isinstance(finding.get(field, ""), str):
                    raise CommandError(f"finding {field} must be a string")
            repos = finding.get("repos", [])
            if (not isinstance(repos, list)
                    or not all(isinstance(item, str) for item in repos)):
                raise CommandError("finding repos must be a list of strings")
            for field in ("categories", "installs"):
                values = finding.get(field, {})
                if not isinstance(values, dict) or not all(
                        isinstance(item, str) for item in values.values()):
                    raise CommandError(
                        f"finding {field} must be an object with string values")
        stats = imports.merge_findings(user, skill_findings)
        self.stdout.write(self.style.SUCCESS(
            f"merged: {stats['links']} link change(s), "
            f"{stats['prompts']} prompt change(s), "
            f"{stats['updated']} updated for {user.email}"))
