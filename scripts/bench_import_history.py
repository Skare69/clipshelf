#!/usr/bin/env python3
"""Write-lock hold time of a legacy import whose history maps are large.

The legacy `seen` and `removed` maps become History rows (up to three per
URL: the URL, its canonical key, and an alias) inside the import's one
write transaction. This benchmark migrates a scratch database, imports a
legacy payload with N `seen` URLs (no links, so nothing is fetched), and
runs one concurrent capture from another user while the import holds the
write lock. It reports the transaction hold time and how long the capture
waited.

The default size is services.MAX_IMPORT_HISTORY, the largest payload that
admission accepts. Larger sizes were legal before the cap, and the cap was
chosen against their measurements: for those runs the script disables the
shared cap check and calls the unchanged engine, so the commit path stays
the one the cap protects.

Usage: python3 scripts/bench_import_history.py [seen_urls]
       (default: services.MAX_IMPORT_HISTORY)
Always run it with CLIPSHELF_DATA_DIR set to a fresh throwaway directory.
A directory that already holds a database is refused, never benchmarked.
"""
import os
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

from django.core.exceptions import ValidationError  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import OperationalError, connection, connections  # noqa: E402

from clipshelf import imports, models, publication, services  # noqa: E402


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else services.MAX_IMPORT_HISTORY
    call_command("migrate", verbosity=0)
    stamp = uuid.uuid4().hex[:8]
    importer = models.User.objects.create_user(
        username=f"imp-{stamp}", email=f"imp-{stamp}@example.com")
    other = models.User.objects.create_user(
        username=f"cap-{stamp}", email=f"cap-{stamp}@example.com")
    services.get_settings().save()
    # A tracking parameter makes each URL differ from its canonical key, so
    # each URL writes the worst case of three History rows.
    seen = {f"https://bench.example/{stamp}/{i}?utm_source=x": "2024-01-01"
            for i in range(n)}
    data = {"links": {}, "seen": seen}
    print(f"busy_timeout: {connection.cursor().execute('PRAGMA busy_timeout').fetchone()[0]} ms")
    real_cap = imports._check_history_cap
    if n > services.MAX_IMPORT_HISTORY:
        # Admission and the engine both refuse over-cap payloads today. The
        # cap was chosen against the pre-cap path, so the run disables the
        # refusal alone and keeps the commit path intact.
        fmt = "legacy"
        args = imports.legacy_payload(data)

        def uncapped(seen, removed):
            return None

        imports._check_history_cap = uncapped
        print(f"seen={n} over cap {services.MAX_IMPORT_HISTORY}: "
              "cap check disabled for the run")
    else:
        fmt, args = imports.parse_upload(data)

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

    publication.record_import_history = history
    services.commit_import_rows = timed_commit
    thread = threading.Thread(target=concurrent_capture)
    thread.start()
    try:
        imports.import_items(user=importer, collection_id=None, **args)
    except ValidationError as exc:
        print(f"seen={n} refused at commit: {exc.messages[0]}")
        in_lock.set()
    finally:
        imports._check_history_cap = real_cap
    thread.join()
    if "s" in hold:
        rows = models.History.objects.filter(user=importer).count()
        print(f"seen={n} history_rows={rows} write_lock_hold={hold['s']:.2f}s")
    print(f"concurrent capture: {capture['result']} after {capture['wait']:.2f}s")


if __name__ == "__main__":
    main()
