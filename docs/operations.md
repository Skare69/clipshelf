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

An imported item with media also writes a `Job`, its `Asset` rows and its
source in the same transaction. The import hashes and measures the asset
files before the transaction starts, so the lock covers only row writes.
Measured on 2026-11-27 with
`python3 scripts/bench_import_assets.py <media_items> <images> <link_items>`
on the same kind of disk, with the query log off as in a deployment:

| Import | Lock with hashing inside | Lock with hashing before |
| --- | --- | --- |
| 5000 items x 16 images (80000 assets) | 52.8 s | 38.5 s |
| 5000 items x 1 image | 34.1 s | 34.9 s |
| 5000 link-only items | not measured | 11.4 s |
| 2000 items x 16 images, 3000 link-only items | not measured | 23.1 s |
| 1000 items x 16 images, 4000 link-only items | not measured | 16.6 s |

In each run above 30 s, a concurrent capture failed with "database is
locked". The writes for each item with media cost most of the lock time,
not the asset count. Thus admission and the import engine refuse an
import with a 400 when it has more than `MAX_IMPORT_MEDIA_ITEMS` (1000)
items with media, or more than `MAX_IMPORT_MEDIA_FILES` (16000) media
files (clipshelf/services.py). An item with media has images, a video or
a cached page. An interpreted legacy item also counts, because the import
fetches its page. The last row of the table is the largest import that
admission accepts. The 10000 history URLs that admission also accepts add
2 s to 3 s (measured separately, see above).

`tiktok-extract.js` and `whatsapp-extract.js` stop one run at the same
caps. The next run exports the remaining posts or messages.

## LLM spend ceilings

Interpretation bills the operator's LLM key, so the endpoints that create
work run under per-user budgets.

One capture POST creates one job per URL in the text
(`clipshelf/services.py`), and the budgets count requests, not URLs. One
job attempt makes at most 3 model calls: the first call, one
doubled-limit retry after a length cap, and one malformed-output retry.
`Job.MAX_ATTEMPTS` (5, `clipshelf/models.py`) bounds one automatic
failure streak: the job blocks at the cap, so one streak costs at most
15 model calls, each with up to 8000 output tokens (admin setting,
default 4000), plus the prompt tokens. A manual "Try again" press
ignores the cap: `Job.requeue` requeues the row and the worker runs it
without an attempts check, so each press adds up to 3 more calls, and a
press on a done job can start a new 5-attempt streak. The retry budget
below bounds the press rate. The worker adds each attempt's usage to
`Job.prompt_tokens` and `Job.completion_tokens`.

Four budgets use the process-local cache (per process, reset on restart;
the same machinery as the invitation and tag-suggestion budgets):

- Captures: `CLIPSHELF_CAPTURE_RATE`, default `30/h/user`, one count per
  capture POST.
- Retries: `CLIPSHELF_JOB_RETRY_RATE`, default `20/h/user`, one count per
  "Try again" press.
- Imports: `CLIPSHELF_IMPORT_RATE`, default `10/h/user`, one count per
  upload POST. A URL-only item queues one interpreted job like a capture.
- Follows: `CLIPSHELF_FOLLOW_RATE`, default `10/h/user`, one count per
  follow create POST. The first listing of a follow queues one capture
  per capped post; an unfollow drops the seen ledger, so a recreate
  loop would re-queue the same posts.

An over-budget request answers HTTP 429 and admits no work. The values
are code defaults read with `getattr` in `clipshelf/views.py`. The token
counters have no spending limit; sum the job rows yourself when you need
a per-user daily figure.

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
