"""Consistent offline backup: drains writers, snapshots DB + media + config."""
import json
import os
import shutil
import sqlite3
from pathlib import Path

import django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from clipshelf import models, services
from clipshelf.management.locks import data_lock

CONFIG_KEYS = ("DEFAULT_FROM_EMAIL", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_TLS",
               "EMAIL_USE_SSL", "EMAIL_HOST_USER", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS")
SECRET_KEYS = ("SECRET_KEY", "EMAIL_HOST_PASSWORD")  # never written into manifest.json


class Command(BaseCommand):
    help = ("Back up database, retained media, instance identity, and the "
            "environment config/secrets needed to restore. Takes the exclusive "
            "data lock: stop web/worker first (that is the controlled write pause).")

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="directory for the backup")
        parser.add_argument("--wait", type=float, default=30.0,
                            help="seconds to wait for the exclusive data lock")

    def handle(self, *args, **options):
        target = Path(options["output"])
        if target.exists() and any(target.iterdir()):
            raise CommandError(f"refusing to write into a non-empty directory: {target}")
        target.mkdir(parents=True, exist_ok=True)
        db_path = Path(str(settings.DATABASES["default"]["NAME"]))

        try:
            with data_lock(exclusive=True, timeout=options["wait"]):
                self._snapshot(db_path, target)
        except TimeoutError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(f"backup written to {target}"))
        self.stdout.write("contents: db.sqlite3 (SQLite backup API snapshot), "
                          "assets/, manifest.json" +
                          (", SECRETS.json (mode 0600)" if (target / "SECRETS.json").exists() else ""))
        if (target / "SECRETS.json").exists():
            self.stdout.write(self.style.WARNING(
                "the backup contains secret material; protect the directory"))

    def _snapshot(self, db_path, target):
        # Online backup API against the live connection: a consistent copy
        # including WAL content, without depending on the file layout.
        from django.db import connection

        connection.ensure_connection()
        dst = sqlite3.connect(str(target / "db.sqlite3"))
        try:
            with dst:
                connection.connection.backup(dst)
        finally:
            dst.close()

        assets = Path(settings.DATA_DIR) / "assets"
        if assets.is_dir():
            shutil.copytree(assets, target / "assets")

        manifest = {
            "created_at": timezone.now().isoformat(),
            "instance_id": str(services.get_settings().instance_id),
            "database": {"engine": "sqlite", "path_name": db_path.name,
                         "sqlite_version": sqlite3.sqlite_version},
            "versions": {"django": django.get_version()},
            "counts": {
                "users": models.User.objects.count(),
                "collections": models.Collection.objects.count(),
                "entries": models.Entry.objects.count(),
                "jobs": models.Job.objects.count(),
                "assets": models.Asset.objects.count(),
            },
            "config": {key: getattr(settings, key) for key in CONFIG_KEYS
                       if getattr(settings, key, None) not in (None, "", [], ())},
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8")

        secrets = {key: getattr(settings, key) for key in SECRET_KEYS
                   if getattr(settings, key, "")}
        if secrets:
            path = target / "SECRETS.json"
            path.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
            os.chmod(path, 0o600)
