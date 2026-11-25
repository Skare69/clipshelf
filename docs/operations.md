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

## Import parse memory budget

`json.load()` is not a streaming parser. It builds every element of the
import body before the shape check runs. The admitted cost has two shapes.

Empty-array floods cost about 26 MiB of peak RSS per MiB of body: the
default 32 MiB ceiling (`CLIPSHELF_MAX_IMPORT_BYTES`) built about 830 MiB
of list objects, and the web process runs waitress with its default four
threads, so four parallel floods asked for about 3.3 GiB. Admission now
counts the structural characters `[ { , :` outside string literals and
refuses a body past `MAX_IMPORT_ELEMENTS` (250000, clipshelf/imports.py)
with a 400. The scanner decodes the bytes exactly like the parser
(UTF-8, UTF-16, UTF-32), so a wide encoding cannot hide elements from
the count. The count walks the file in 64 KiB chunks, so the scan adds
no peak memory. The largest admitted flood holds 125000 empty arrays and
parses to about 9 MiB of Python objects. The worker re-parses the staged
file under the same cap.

String contents do not count as elements. A body of a few giant string
literals is admitted and rides the upload ceiling alone, and one astral
character (an emoji is enough) makes CPython store the whole string as
four bytes per character: a 4 MiB string then occupies 16777280 bytes.
Poisoned 64 KiB strings measure about 9 MiB of peak RSS per MiB of body,
about 286 MiB at the 32 MiB ceiling. Four parallel string-heavy imports
ask for about 1.2 GiB.

Size the container memory for the web and worker baseline plus about
1.25 GiB of import headroom, or set a lower `CLIPSHELF_MAX_IMPORT_BYTES`
in the compose environment when the host is smaller: string-heavy parse
memory stays near 9 MiB of RSS per MiB of body at any ceiling.
Re-measure after a Python upgrade with
`python3 scripts/bench_import_parse.py`; run it once with `--arrays` and
once with `--strings`. The script prints the peak RSS per body size and
needs about 850 MB free for the 32 MiB probes. Measured on 2026-11-24
with CPython 3.13.5: arrays 4 MiB body 108 MiB peak RSS, 8 MiB 216 MiB,
16 MiB 433 MiB, 32 MiB 833 MiB; poisoned strings 4 MiB 34 MiB, 8 MiB
70 MiB, 16 MiB 142 MiB, 32 MiB 286 MiB.

## Import write-lock budget

An import writes all its rows in one SQLite write transaction. Other
users' writes (captures, job updates) wait up to the 30 s `busy_timeout`,
then fail. A legacy `seen` or `removed` URL writes up to three `History`
rows. Admission and the import engine refuse more than
`MAX_IMPORT_HISTORY` (10000, clipshelf/services.py) `seen` and `removed`
URLs together with a 400. The cap limits the count of history rows, not
the lock time. The same transaction also writes the links and prompts of
the import, and the lock time depends on the storage.

Measured on 2026-11-25 with
`python3 scripts/bench_import_history.py <urls>` on the local disk of a
development container, with an import that has no links or prompts:
10000 URLs (30000 rows) held the write lock for 2.3 s to 2.9 s, and
125000 URLs (375000 rows) held it for 32 s, so a concurrent capture
failed. The script does not measure imports with links, or the deployed
storage. Run it with `CLIPSHELF_DATA_DIR` set to a throwaway directory.

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
