#!/usr/bin/env python3
"""Write-lock hold time of an import that carries many image assets.

Every imported image becomes a published file and an Asset row, written in
the import's one write transaction (services.commit_import_rows). This
benchmark migrates a scratch database, imports N items with M base64 PNG
images each (no fetches), and runs one concurrent capture from another
user while the import holds the write lock. It reports the transaction
hold time, the share of it spent hashing asset files inside the lock, and
how long the capture waited.

Usage: python3 scripts/bench_import_assets.py [items] [images] [side_px]
       (default: 5000 items x 16 images of 4x4 px)
side_px sets the PNG edge in pixels; the pixels are random, so a file holds
about 3 * side_px**2 bytes. Always run it with CLIPSHELF_DATA_DIR set to a
fresh throwaway directory. A directory that already holds a database is
refused, never benchmarked.

Staging runs before the transaction: each item lands in its own subdirectory
of the import's staging directory, so same-named slides never collide and the
staging name probe stays one per file (linear in the item count). The commit
path that the lock covers stays unchanged.
"""
import base64
import io
import os
import random
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data_dir = os.environ.get("CLIPSHELF_DATA_DIR")
if not data_dir:
    sys.exit("set CLIPSHELF_DATA_DIR to a throwaway directory")
if list(Path(data_dir).glob("db.sqlite3*")):
    sys.exit(f"{data_dir} already holds a database; use a fresh directory")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import OperationalError, connection, connections  # noqa: E402
from PIL import Image  # noqa: E402

from clipshelf import asset_files, imports, models, publication, services  # noqa: E402

# The checked-in settings refuse DEBUG off on this SQLite build, and DEBUG on
# logs and formats every query. A deployment runs DEBUG off: turn the query
# log off at runtime so the hold time is the deployed one.
settings.DEBUG = False


def png_b64(side, rng):
    buf = io.BytesIO()
    Image.frombytes("RGB", (side, side), rng.randbytes(3 * side * side)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def main():
    n_items = int(sys.argv[1]) if len(sys.argv) > 1 else services.MAX_IMPORT_ITEMS
    n_images = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    side = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    call_command("migrate", verbosity=0)
    stamp = uuid.uuid4().hex[:8]
    importer = models.User.objects.create_user(
        username=f"imp-{stamp}", email=f"imp-{stamp}@example.com")
    other = models.User.objects.create_user(
        username=f"cap-{stamp}", email=f"cap-{stamp}@example.com")
    services.get_settings().save()
    rng = random.Random(0)
    pool = [png_b64(side, rng) for _ in range(n_images)]
    # images=0 imports link-only items: no assets, the job loop fetches later.
    items = [{"url": f"https://bench.example/{stamp}/{i}",
              **({"images": [{"b64": b64} for b64 in pool]} if pool else {})}
             for i in range(n_items)]
    print(f"busy_timeout: {connection.cursor().execute('PRAGMA busy_timeout').fetchone()[0]} ms")
    print(f"items={n_items} images_per_item={n_images} "
          f"png_bytes={len(base64.b64decode(pool[0])) if pool else 0}")

    in_lock = threading.Event()
    capture = {}
    real_history = publication.record_import_history

    def history(**kw):
        in_lock.set()  # the transaction is open and holds the write lock
        return real_history(**kw)

    def concurrent_capture():
        in_lock.wait()
        start = time.monotonic()
        try:
            services.accept_capture(other, str(uuid.uuid4()),
                                    "https://capture.example/a", None)
            capture["result"] = "ok"
        except OperationalError as exc:
            capture["result"] = f"failed: {exc}"
        finally:
            connections.close_all()
        capture["wait"] = time.monotonic() - start

    real_commit = services.commit_import_rows
    hold = {}

    def timed_commit(*a, **kw):
        start = time.monotonic()
        try:
            return real_commit(*a, **kw)
        finally:
            hold["s"] = time.monotonic() - start

    real_hash = asset_files._file_sha256
    hashing = {"in_lock_s": 0.0, "in_lock_files": 0, "outside_files": 0}

    def timed_hash(path):
        start = time.monotonic()
        try:
            return real_hash(path)
        finally:
            if connection.in_atomic_block:
                hashing["in_lock_s"] += time.monotonic() - start
                hashing["in_lock_files"] += 1
            else:
                hashing["outside_files"] += 1

    real_import_export = imports.acquisition.import_export
    staging = {}

    def timed_staging(*a, **kw):
        start = time.monotonic()
        try:
            return real_import_export(*a, **kw)
        finally:
            staging["s"] = time.monotonic() - start

    publication.record_import_history = history
    services.commit_import_rows = timed_commit
    asset_files._file_sha256 = timed_hash
    imports.acquisition.import_export = timed_staging
    thread = threading.Thread(target=concurrent_capture)
    thread.start()
    start = time.monotonic()
    try:
        result = imports.import_items(user=importer, collection_id=None, items=items)
    finally:
        in_lock.set()
        thread.join()
    total = time.monotonic() - start
    print(f"import_total={total:.2f}s staging={staging.get('s', 0):.2f}s "
          f"assets={result['counts']['assets']} "
          f"asset_rows={models.Asset.objects.count()}")
    print(f"write_lock_hold={hold['s']:.2f}s "
          f"hashing_in_lock={hashing['in_lock_s']:.2f}s "
          f"({hashing['in_lock_files']} files; "
          f"{hashing['outside_files']} hashed outside the lock)")
    print(f"concurrent capture: {capture['result']} after {capture['wait']:.2f}s")


if __name__ == "__main__":
    main()
