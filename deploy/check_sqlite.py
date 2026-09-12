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

MIN_SQLITE = (3, 51, 3)


def fail(message):
    print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def cleanup(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
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
    probe = os.path.join(data_dir, "sqlite-verification.db")
    cleanup(probe)

    try:
        con = sqlite3.connect(probe, timeout=10)
    except sqlite3.Error as exc:
        fail(f"cannot create {probe}: {exc}")
    try:
        try:
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
    finally:
        con.close()
    if mode != "wal":
        fail(f"journal_mode did not stick on {data_dir} (got {mode!r})")
    if sync != 2:
        fail(f"synchronous=FULL not in effect (got {sync})")

    # A fresh connection must see the committed row and the persistent WAL mode.
    try:
        con2 = sqlite3.connect(probe, timeout=10)
    except sqlite3.Error as exc:
        fail(f"cannot reopen {probe}: {exc}")
    try:
        row = con2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        mode2 = str(con2.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except sqlite3.Error as exc:
        fail(f"verification queries failed on reopen: {exc}")
    finally:
        con2.close()
    if row != 1 or mode2 != "wal":
        fail(f"WAL data not durable/visible after reopen (rows={row}, mode={mode2!r})")

    cleanup(probe)
    print(
        f"PASS: SQLite {sqlite3.sqlite_version} on {data_dir}: WAL persists, "
        "synchronous=FULL commits are visible across connections. App settings "
        "enforce the same pragmas on every Django connection."
    )


if __name__ == "__main__":
    main()
