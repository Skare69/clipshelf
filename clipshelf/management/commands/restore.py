"""Restore a backup into an isolated data directory; refuses live overwrite."""
import json
import shutil
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from clipshelf.management.locks import data_lock

ASSETS = "assets"


class Command(BaseCommand):
    help = ("Restore a clipshelf backup into the current (empty) data directory. "
            "Refuses to touch a live instance. Run only on an isolated instance "
            "with web/worker stopped.")

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="backup directory")
        parser.add_argument("--wait", type=float, default=30.0)

    def handle(self, *args, **options):
        backup = Path(options["input"])
        db_src = backup / "db.sqlite3"
        manifest_path = backup / "manifest.json"
        if not db_src.is_file() or not manifest_path.is_file():
            raise CommandError(f"not a clipshelf backup: {backup} (need db.sqlite3 + manifest.json)")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        target_db = Path(str(settings.DATABASES["default"]["NAME"]))
        tables = 0
        if target_db.exists():
            con = sqlite3.connect(str(target_db))
            try:
                tables = con.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            finally:
                con.close()
        else:
            # The configured database may not be a plain file: ask the live
            # connection, but only when it really is the restore target.
            from django.db import connection

            if str(connection.settings_dict.get("NAME")) == str(target_db):
                try:
                    connection.ensure_connection()
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'")
                        tables = cursor.fetchone()[0]
                except Exception:  # unreachable database: nothing live to protect
                    tables = 0
        if tables:
            raise CommandError(
                f"refusing live overwrite: {target_db} already contains "
                f"{tables} table(s); restore only into an empty data directory")

        try:
            with data_lock(exclusive=True, timeout=options["wait"]):
                target_db.parent.mkdir(parents=True, exist_ok=True)
                for suffix in ("-wal", "-shm"):
                    target_db.with_name(target_db.name + suffix).unlink(missing_ok=True)
                shutil.copy2(db_src, target_db)
                assets_src = backup / ASSETS
                if assets_src.is_dir():
                    shutil.copytree(assets_src, Path(settings.DATA_DIR) / ASSETS,
                                    dirs_exist_ok=True)
        except TimeoutError as exc:
            raise CommandError(str(exc))

        instance_id = manifest.get("instance_id", "?")
        self.stdout.write(self.style.SUCCESS(
            f"restored backup from {manifest.get('created_at', '?')} "
            f"(instance_id={instance_id}, counts={manifest.get('counts', {})})"))
        secrets_needed = []
        secrets_path = backup / "SECRETS.json"
        if secrets_path.is_file():
            secrets_needed = list(json.loads(secrets_path.read_text(encoding="utf-8")).keys())
        self.stdout.write(
            "set these environment/config values before serving if not already: "
            + (", ".join(secrets_needed) if secrets_needed else "(none recorded)"))
