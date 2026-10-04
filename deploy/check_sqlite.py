#!/usr/bin/env python3
"""Operator verification of the Clipshelf SQLite runtime. Run inside the image.

Proves, without trusting any image tag or Dockerfile claim:
  1. runtime SQLite includes the WAL-reset fix (>= 3.51.3)
  2. WAL journal mode engages and persists on the real data volume
  3. synchronous=FULL is accepted and a committed row is visible to a fresh
     connection through the WAL

Usage:
  docker compose --profile ops run --rm verify
  # or directly:
  docker compose exec web python deploy/check_sqlite.py
"""
import os
import sqlite3
import sys
import tempfile

MIN_SQLITE = (3, 51, 3)


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def remove_own_probe(probe):
    """Remove only files this run created; mkstemp made `probe` via O_EXCL,
    so the -wal/-shm sidecars of that unique name can never be user files."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(probe + suffix)
        except FileNotFoundError:
            pass


def main():
    print(f"python {sys.version.split()[0]}; runtime SQLite {sqlite3.sqlite_version}")
    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if version < MIN_SQLITE:
        fail(
            f"SQLite {sqlite3.sqlite_version} lacks the WAL-reset corruption fix "
            f"(need >= {'.'.join(map(str, MIN_SQLITE))}); see "
            "https://www.sqlite.org/wal.html. The image ships a pinned fixed "
            "build; if you replaced it, install a verified fixed build."
        )

    data_dir = os.environ.get("CLIPSHELF_DATA_DIR", "/data")
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as exc:
        fail(f"cannot use data dir {data_dir}: {exc} (chown it to the compose UID/GID)")
    # Unique probe name via mkstemp (O_EXCL): it can never collide with or
    # overwrite an existing file, and nothing pre-existing is ever removed.
    try:
        fd, probe = tempfile.mkstemp(
            prefix="sqlite-verification-", suffix=".db", dir=data_dir
        )
        os.close(fd)
    except OSError as exc:
        fail(f"cannot create a unique probe file in {data_dir}: {exc}")

    con = None
    try:
        try:
            con = sqlite3.connect(probe, timeout=10)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA busy_timeout=10000")
            con.execute("CREATE TABLE t(x INTEGER)")
            con.execute("INSERT INTO t VALUES (1)")
            con.commit()
            mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            sync = int(con.execute("PRAGMA synchronous").fetchone()[0])
        except sqlite3.Error as exc:
            fail(f"verification queries failed on {data_dir}: {exc}")
        if mode != "wal":
            fail(f"journal_mode did not stick on {data_dir} (got {mode!r})")
        if sync != 2:
            fail(f"synchronous=FULL not in effect (got {sync})")

        # A fresh connection must read the committed row through the live WAL
        # while this writer connection is still open; after the last
        # connection closes, SQLite checkpoints and removes the WAL, so a
        # reopen at that point would prove nothing about WAL visibility.
        con2 = None
        try:
            con2 = sqlite3.connect(probe, timeout=10)
            row = con2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
            mode2 = str(con2.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        except sqlite3.Error as exc:
            fail(f"fresh-connection read through the live WAL failed: {exc}")
        finally:
            if con2 is not None:
                con2.close()
        if row != 1 or mode2 != "wal":
            fail(
                "fresh connection did not see the committed WAL row "
                f"(rows={row}, mode={mode2!r})"
            )
    finally:
        if con is not None:
            con.close()
        remove_own_probe(probe)
    print(
        f"PASS: SQLite {sqlite3.sqlite_version} on {data_dir}: WAL journal mode "
        "engages and persists, synchronous=FULL is in effect, and a fresh "
        "connection read the committed row through the live WAL while the "
        "writer connection stayed open. This probe uses raw sqlite3 "
        "connections only; it does not exercise Django connection pragmas."
    )


if __name__ == "__main__":
    main()
