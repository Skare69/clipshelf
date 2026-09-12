"""Read-only migration of a legacy library.json into a user's Personal collection."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from clipshelf import models, worker

MAX_LIBRARY_BYTES = 64 * 1024 * 1024  # legacy file guard; upload ceiling is separate

LEGACY_FIELDS = ("title", "desc", "sources", "tags", "found", "interpreted",
                 "pending", "cat", "install", "archived", "stars")


class Command(BaseCommand):
    help = ("Import a legacy library.json into the given user's Personal "
            "collection. Input and cache are read-only; interpret_cmd is "
            "recorded in the manifest, never activated. Exact repeat is a no-op.")

    def add_arguments(self, parser):
        legacy_cache = Path.cwd() / "cache"
        parser.add_argument("--user", required=True, help="existing account email")
        parser.add_argument("--input", required=True, help="path to library.json")
        parser.add_argument("--cache", default=str(legacy_cache),
                            help=f"legacy cache directory (default: {legacy_cache})")
        parser.add_argument("--dry-run", action="store_true",
                            help="build the manifest and compare, import nothing")

    def handle(self, *args, **options):
        user = models.User.objects.filter(email__iexact=options["user"].strip()).first()
        if user is None:
            raise CommandError(f"no account for {options['user']}")
        source = Path(options["input"])
        if not source.is_file():
            raise CommandError(f"input not found: {source}")
        if source.stat().st_size > MAX_LIBRARY_BYTES:
            raise CommandError(f"input larger than {MAX_LIBRARY_BYTES} bytes; refusing")
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CommandError("library.json must be a JSON object")

        items = []
        for url, entry in data.get("links", {}).items():
            item = {"url": url}
            item.update({k: entry[k] for k in LEGACY_FIELDS if entry.get(k) not in (None, "", [])})
            for k in ("images", "video", "cache"):
                if entry.get(k):
                    item[k] = entry[k]
            items.append(item)
        prompts = [p for p in data.get("prompts", {}).values() if isinstance(p, dict)]

        result = worker.import_items(
            user=user, collection_id=None, items=items, prompts=prompts,
            seen=data.get("seen", {}), removed=data.get("removed", {}),
            cache_dir=options["cache"], origin="legacy-import",
            dry_run=options["dry_run"])

        manifest = result["manifest"]
        cmd = (data.get("settings") or {}).get("interpret_cmd", "")
        manifest["legacy_settings"] = {"interpret_cmd": cmd, "activated": False}
        if not options["dry_run"] and result["created"]:
            record = models.ImportRecord.objects.get(user=user, input_digest=result["digest"])
            record.manifest = manifest
            record.save(update_fields=["manifest"])

        counts = manifest.get("counts", {})
        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(
                f"dry run: digest={result['digest'][:16]} counts={counts} "
                f"missing_media={counts.get('missing_media', 0)} (nothing imported)"))
        elif result["created"]:
            self.stdout.write(self.style.SUCCESS(
                f"imported: digest={result['digest'][:16]} counts={counts} "
                f"result={result['counts']} into personal collection "
                f"{result['collection_id']} (interpret_cmd recorded, not activated)"))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"unchanged: identical input already imported "
                f"(digest={result['digest'][:16]}, counts={counts})"))
