"""Restore a backup into an isolated data directory; refuses live overwrite.

Publication is crash-safe: the database and assets are first staged next to
their final locations (same volume, so publication is a rename, never a
partial in-place copy), the emptiness guard is rechecked under the exclusive
lock, an existing assets/ directory is renamed aside to assets.gen-<id>
(preserved intact — never deleted, never merged), and the database file is
swapped in last with one atomic rename. The database is the commit point: a
crash before it leaves no database behind, so rerunning restore always
retries safely. Failed or crashed attempts leave their staged files and
assets.gen-* directories on disk; cleanup is a manual operator decision.
"""
import json
import os
import shutil
import sqlite3
import time
import uuid
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
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommandError(
                f"not a clipshelf backup: unreadable manifest.json: {exc}") from exc

        target_db = Path(str(settings.DATABASES["default"]["NAME"]))
        data_dir = Path(settings.DATA_DIR)
        tables = self._tables(target_db)
        if tables:
            raise CommandError(
                f"refusing live overwrite: {target_db} already contains "
                f"{tables} table(s); restore only into an empty data directory")

        # Stage complete copies before holding any lock; a failure here has
        # touched nothing published.
        data_dir.mkdir(parents=True, exist_ok=True)
        target_db.parent.mkdir(parents=True, exist_ok=True)
        token = f"{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        staged_db = target_db.with_name(target_db.name + f".restore-{token}")
        staged_assets = data_dir / f"restore-staging-{token}"
        has_assets = (backup / ASSETS).is_dir()
        try:
            shutil.copy2(db_src, staged_db)
            con = sqlite3.connect(str(staged_db))
            try:
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                con.close()
            if has_assets:
                shutil.copytree(backup / ASSETS, staged_assets)
        except sqlite3.Error as exc:
            raise CommandError(
                f"backup db.sqlite3 is not a readable SQLite database: {exc}") from exc
        except OSError as exc:
            raise CommandError(
                f"staging failed before anything was published: {exc}; leftover "
                f"staged files may remain at {staged_db} or {staged_assets}") from exc

        try:
            with data_lock(exclusive=True, timeout=options["wait"]):
                # Recheck under the lock: the pre-lock check cannot see a
                # server that populated the database after it.
                tables = self._tables(target_db)
                if tables:
                    raise CommandError(
                        f"refusing live overwrite: {target_db} already contains "
                        f"{tables} table(s); restore only into an empty data directory")
                for suffix in ("-wal", "-shm"):
                    target_db.with_name(target_db.name + suffix).unlink(missing_ok=True)
                # Generation cutover: whatever the target held is renamed
                # aside intact; the complete staged copy becomes assets/ in
                # one atomic rename.
                existing = data_dir / ASSETS
                if existing.exists() or existing.is_symlink():
                    os.rename(existing, data_dir / f"assets.gen-{token}")
                if has_assets:
                    os.rename(staged_assets, data_dir / ASSETS)
                os.replace(staged_db, target_db)  # atomic commit: database appears last
        except TimeoutError as exc:
            raise CommandError(str(exc))
        except OSError as exc:
            raise CommandError(
                f"publication failed before the database swap: {exc}; any previous "
                f"assets/ directory was renamed aside (assets.gen-*, never deleted) "
                f"and rerunning restore retries safely") from exc

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

    def _tables(self, target_db):
        """Count user tables of the restore target; 0 when absent, empty, or
        unreadable (a crash-truncated file is debris, not a live instance)."""
        if target_db.exists():
            con = sqlite3.connect(str(target_db))
            try:
                return con.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
            except sqlite3.DatabaseError:
                return 0
            finally:
                con.close()
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
                    return cursor.fetchone()[0]
            except Exception:  # unreachable database: nothing live to protect
                return 0
        return 0
