#!/usr/bin/env python3
"""Clipshelf container entrypoint: explicit operations, shell-free.

First run needs no operator commands: `clipshelf.py serve` and `worker` apply
migrations themselves, and the web UI serves a setup wizard at /setup that
creates the first administrator — like Jellyfin or the *arr apps.

Ops (first argument, default `serve`):
  serve              run the single waitress web process on 0.0.0.0:8000
  worker [--once]    run the one background coordinator (optionally one pass)
  migrate            apply Django migrations to the data volume (run
                     automatically by serve/worker; kept for operators)
  bootstrap          create an administrator via CLI (requires
                     CLIPSHELF_ADMIN_EMAIL; recovery path beside the wizard)
  check              run Django system checks (diagnostics, no production gates)

Production gates (skipped only with CLIPSHELF_DEBUG=1):
  - runtime SQLite >= 3.51.3 (WAL-reset fix); never assumed from the image tag

Origin/secret-key/allowed-hosts validation lives in app settings, which fail
with a precise message. No anonymous or automatic administrator is ever
created.
"""
import os
import sqlite3
import sys

MIN_SQLITE = (3, 51, 3)
DATA_DIR = os.environ.get("CLIPSHELF_DATA_DIR", "/data")
BIND = "0.0.0.0:8000"  # inside the container always all interfaces; compose maps the port


def fail(message):
    print(f"entrypoint: {message}", file=sys.stderr)
    sys.exit(1)


def ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, ".entrypoint-write-probe")
        with open(probe, "x", encoding="ascii") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        fail(
            f"data directory {DATA_DIR} is not writable by uid/gid "
            f"{os.getuid()}/{os.getgid()}: {exc}. Chown the host dataset to the "
            "CLIPSHELF_UID/CLIPSHELF_GID configured in compose.yaml."
        )


def production_gates():
    if os.environ.get("CLIPSHELF_DEBUG") == "1":
        print("entrypoint: CLIPSHELF_DEBUG=1 — development mode, production gates skipped")
        return
    # Origin, secret key and allowed hosts are validated by app settings,
    # which raise ImproperlyConfigured with a precise message.
    if not os.environ.get("CLIPSHELF_SMTP_HOST"):
        print(
            "entrypoint: warning CLIPSHELF_SMTP_HOST unset — invitation and "
            "password-recovery mail cannot send (ordinary login still works)",
            file=sys.stderr,
        )
    version = tuple(int(part) for part in sqlite3.sqlite_version.split("."))
    if version < MIN_SQLITE and os.environ.get("CLIPSHELF_SQLITE_VERIFIED") != "1":
        fail(
            f"runtime SQLite {sqlite3.sqlite_version} lacks the WAL-reset fix "
            f"(need >= {'.'.join(map(str, MIN_SQLITE))}); see "
            "https://www.sqlite.org/wal.html. This image builds a pinned fixed "
            "SQLite; if you swapped it, install a verified fixed build, then — "
            "and only then — set CLIPSHELF_SQLITE_VERIFIED=1."
        )


def main():
    argv = sys.argv[1:]
    op = argv[0] if argv else "serve"
    rest = argv[1:]
    ensure_data_dir()
    print(
        f"entrypoint: runtime SQLite {sqlite3.sqlite_version} "
        f"(minimum {'.'.join(map(str, MIN_SQLITE))}, uid {os.getuid()})"
    )

    launcher = [sys.executable, "clipshelf.py"]
    if op == "serve":
        production_gates()
        cmd = [*launcher, "serve", BIND]
    elif op == "worker":
        production_gates()
        if rest and rest != ["--once"]:
            fail(f"worker accepts no arguments besides --once, got {rest}")
        cmd = [*launcher, "worker", *rest]
    elif op == "migrate":
        production_gates()
        cmd = [*launcher, "migrate"]
    elif op == "check":
        cmd = [*launcher, "check"]
    elif op == "bootstrap":
        production_gates()
        email = os.environ.get("CLIPSHELF_ADMIN_EMAIL", "")
        if not email:
            fail(
                "bootstrap requires CLIPSHELF_ADMIN_EMAIL; this is an explicit "
                "operator action and no anonymous admin is created"
            )
        cmd = [*launcher, "bootstrap_admin", "--email", email]
    else:
        fail(
            f"unknown op {op!r}; expected serve | worker [--once] | migrate | "
            "bootstrap | check (no shell access is provided)"
        )

    print(f"entrypoint: exec {' '.join(cmd)}", flush=True)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
