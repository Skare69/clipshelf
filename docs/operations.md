# Operations

This file lists the operator tasks: staging cleanup, backup, and restore.

## Staging retention and cleanup

Acquisition and import work spools bytes under `<data dir>/staging/` before
publication. Everything there is owned: a job owns `staging/job-<id>` (its
rejected or unpublished media, up to the 256 MiB direct-link ceiling), an
import owns its spooled upload JSON plus a `staging/import-<hex>` media
directory for its fetched bytes. Active work is never eligible for cleanup;
finished work is kept for 30 days after completion for audit and replay.
`python clipshelf.py clean` lists those candidates (paths and sizes) and
deletes nothing. `python clipshelf.py clean --execute` takes the exclusive
data lock (stop web/worker first — the controlled write pause), rechecks each
candidate under the lock, and deletes only paths that are still expired,
skipping (printing, never deleting) anything reactivated after listing.
Unowned staging files are never touched by the command — inspect
and remove those by hand after checking no import or job refers to them.
In Docker the image entrypoint accepts only `serve`, `worker`, `migrate`,
`bootstrap` and `check`, so name Python explicitly:

```
docker compose run --rm --entrypoint python web clipshelf.py clean --execute
```

Verification leftovers on a development machine (regression fixtures and
restore drills: `clipshelf-*`, `cs-src-*`, `restore-*` directories and probe
databases in the OS temp directory) are disposable by policy; the test suite
removes its own fixtures, and the server never scans the OS temp directory.

## Backup and restore

`backup` writes a new directory (the path must not exist yet): a SQLite
backup API snapshot `db.sqlite3`, `assets/`, `manifest.json`, and
`SECRETS.json` (mode 0600) when the instance has secrets — protect it.
Everything clipshelf creates is private to the running account: the process
pins umask 077, so the data directory is 0700 and `db.sqlite3`, the WAL/SHM
sidecars, assets and backup contents are 0600/0700. Directories created by
older versions keep their old modes — run `chmod -R go-rwx` on the data
directory and on existing backups once.
`restore` accepts only an empty, isolated data directory and refuses one that
already holds a database. Both take the exclusive data lock: stop web and
worker first.

```
CLIPSHELF_DATA_DIR=./data .venv/bin/python clipshelf.py backup --output /backups/clipshelf-2026-10-06
CLIPSHELF_DATA_DIR=./restored .venv/bin/python clipshelf.py restore --input /backups/clipshelf-2026-10-06
```

In Docker, pass the entrypoint and mount the backup path. The host backup
directory and the empty restore directory must belong to
`CLIPSHELF_UID`/`CLIPSHELF_GID`:

```
docker compose stop web worker
docker compose run --rm -v /tank/backups:/backups --entrypoint python web clipshelf.py backup --output /backups/clipshelf-2026-10-06
CLIPSHELF_DATA_DIR=/tank/clipshelf-restore docker compose run --rm -v /tank/backups:/backups:ro --entrypoint python web clipshelf.py restore --input /backups/clipshelf-2026-10-06
```

Restore prints the secrets to set before you serve the restored data: put
`SECRET_KEY` from `SECRETS.json` into `CLIPSHELF_SECRET_KEY` and
`EMAIL_HOST_PASSWORD` into `CLIPSHELF_SMTP_PASSWORD`.
