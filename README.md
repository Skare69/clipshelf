# clipshelf

[![tests](https://github.com/Skare69/clipshelf/actions/workflows/ci.yml/badge.svg)](https://github.com/Skare69/clipshelf/actions/workflows/ci.yml)

Share a link from your phone and your own server fetches what is actually in the
post: TikTok photo carousels slide by slide with their audio, video with its ASR
captions, pages as text. A vision model you host turns that into typed repo,
prompt and guide entries — not tags. The Android app queues shares while offline
and delivers them when it can. Self-hosted and account-based. A local model
needs no third-party service when optional screening is off.

![clipshelf](screenshot.jpg)

## What it is

| Piece | What it does |
|---|---|
| Django app (`clipshelf/`) | accounts, collections, capture receipts, library/search, source viewing, admin |
| Worker (`clipshelf.py worker`) | acquisition (pages, TikTok video/carousels), interpretation, retries |
| Android app (`android/`) | share-sheet capture with a durable outbox, background delivery, online browsing |
| Container (`Dockerfile`, `compose.yaml`) | one image, web + worker roles, SQLite and retained media on one data volume |
| `tiktok-extract.js` | browser export for login-required posts; the server never takes TikTok cookies |
| `whatsapp-extract.js` | browser export of one WhatsApp Web chat (links, images, notes) as a `library.json` import; re-runs export only new messages |

The Android outbox holds at most 500 rows and 8 MiB of UTF-8 share text across
all accounts on the phone. One share may hold at most 32768 UTF-8 bytes and 50
URLs. A full outbox rejects new shares and never evicts stored rows; delivered
rows count until you clear them, and paused or rejected rows can be deleted.
Android OS backup is off (`android:allowBackup="false"`), so undelivered shares
remain on this phone only.

Data lives in one directory (`CLIPSHELF_DATA_DIR`): `db.sqlite3` plus `assets/`
(retained pages, images, video). The worker fetches sources and calls the model
endpoint you configure. Optional TypeSafe screening also sends capture material
and findings to its service.

## Run it locally

```
python3.13 -m venv .venv && .venv/bin/python -m pip install --require-hashes -r requirements.lock
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data .venv/bin/python clipshelf.py serve 127.0.0.1:8000   # shell 1
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data .venv/bin/python clipshelf.py worker                 # shell 2
```

Open the URL and complete the setup wizard — it creates the first
administrator. If an administrator locks themselves out, `bootstrap_admin
--email you@example.com` remains the CLI recovery path and prints a single-use
link. In development mail goes to the console; production needs a real SMTP
relay (`CLIPSHELF_SMTP_*`) for invitations and password recovery.

## Deploy on the NAS

Published image (linux/amd64 + linux/arm64), built and verified by CI:
`ghcr.io/skare69/clipshelf:0.9.0`. The signed Android APK is attached to each
[release](https://github.com/Skare69/clipshelf/releases) — there is no app
store, you sideload it.

Android APK versions come from the release tag: `versionName` is the tag
without its `v`, and `versionCode` is `major*1_000_000 + minor*1_000 + patch`
so later tags always sort above earlier installs. Untagged builds must pass
`-PversionName=X.Y.Z` or the build fails.

```
cp deploy/clipshelf.env.example .env      # fill in image, UID/GID, data dir, port
docker compose up -d
```

The container migrates itself on start; open the web UI and complete the setup
wizard to create the first administrator. `docker compose --profile ops run
--rm verify` optionally re-checks the SQLite WAL/synchronous settings. The
image runs as an unprivileged UID/GID you choose, mounts one local dataset, and
publishes the web port on the LAN — reaching it remotely is your tailnet's job,
not the app's.

### Configuration and hardening

Nothing is required beyond image, UID/GID, data dir and port. The secret key is
generated into the data directory on first start. Once SMTP is configured, set
`CLIPSHELF_ORIGIN` to the URL users open (e.g. `http://nas.example:8000`):
account mail never builds links from the request's Host header, so without it
confirmation, reset and signup mails carry path-only links, and
`clipshelf.py check` warns. `CLIPSHELF_ALLOWED_HOSTS`
defaults to `*` because the expected deployment is a LAN with remote access
behind a VPN. CSRF protection is off by default, like Jellyfin and the *arr
services; cookie mutations stay protected by `SameSite=Lax` sessions
regardless. Setting `CLIPSHELF_CSRF=1` turns CSRF verification on and then
requires `CLIPSHELF_ORIGIN` or a concrete `CLIPSHELF_ALLOWED_HOSTS`. Setting
`CLIPSHELF_ORIGIN` to an `https://` URL additionally enables the HTTPS
redirect, HSTS and secure cookies — set it only when a TLS ingress actually
terminates in front of the app. Wildcard hosts permit DNS rebinding; narrow
them by setting `CLIPSHELF_ALLOWED_HOSTS`.

### Dependency and build pinning

`requirements.txt` holds the version ranges. CI, the published image and the
local setup above install one hash-locked resolution of it — `requirements.lock`,
with per-platform wheel hashes for Windows development and linux/amd64 + arm64 —
so the audited set is the shipped set:

- `python scripts/lock_deps.py check` fails when `requirements.txt` would
  resolve differently from the lock (CI runs this gate): a changed pin, a
  missing pin, or an extra pin in the lock that is unrelated to the current
  resolution. The one known exception is Django's win32-only `tzdata` marker
  dependency, which may be present in the lock but absent from a
  non-Windows resolution. It also fails when any shipped platform (Windows,
  linux/amd64, linux/arm64) lacks a wheel matching the lock's hashes.
- `python scripts/lock_deps.py make` regenerates the lock — after changing
  `requirements.txt`, or when the check fails on a new upstream release. It
  writes nothing when any platform has no wheel for a pin.
- Both commands resolve only releases uploaded at least 7 days ago, so a new
  upstream release enters the lock (and turns the check red) one week after it
  appears. A newer pin already in the lock stays.
  `python scripts/lock_deps.py make --fresh django` admits a security release
  of `django` at once.
- `pip-audit` in CI audits the lock directly (`--no-deps`), never a fresh
  re-resolution.

The image base is pinned by manifest digest (`python:3.13-slim@sha256:...`)
and every GitHub Action is pinned to a commit SHA. Bump these deliberately,
one review per bump; the publish workflow additionally verifies the built
image's installed packages against the lock before pushing.
### Staging retention and cleanup

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

### Backup and restore

`backup` writes a new directory (the path must not exist yet): a SQLite
backup API snapshot `db.sqlite3`, `assets/`, `manifest.json`, and
`SECRETS.json` (mode 0600) when the instance has secrets — protect it.
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

## The loop

1. **Capture** — share to the Android app (saved on the phone first, delivered in
   the background) or paste into the web UI. Each capture returns a receipt; a
   retry with the same request id never duplicates it.
2. **Acquire** — the worker resolves the URL and fetches what is public: page
   text, TikTok video with its ASR captions, photo carousels slide by slide.
   Blocked or login-only posts stay saved with an honest warning and a retry or
   `tiktok.json` import path.
3. **Interpret** — one admin-managed OpenAI-compatible endpoint (any local or
   remote vision model) reads the acquired material and returns repos, prompts,
   links and categories, with warnings for anything it could not see or verify.
   Pick a provider from the list — OpenAI, Anthropic, Gemini, OpenRouter, Groq,
   Mistral, xAI, Z.AI, Ollama, llama.cpp, LM Studio, vLLM, or anything else you type —
   and the model names are fetched from that endpoint rather than guessed.
4. **Browse** — Library and Inbox per collection, search, categories, source
   viewing, retry. Collections are the access boundary: everyone has a private
   Personal collection, shared collections have explicit members.

Captured pages are untrusted input. Optionally, clipshelf screens each capture
for prompt injection with a TypeSafe System One battery before it reaches the
model, and screens its findings before publishing. Paste the key into **Admin →
Screening** (or set `TYPESAFE_API_KEY` in the compose `.env`) and press Test.
Screening degrades to a visible warning on failure, and without a key nothing
changes. A job blocked by the guard rail is badged `guardrail` in the inbox;
one that ran with warnings is badged `screened`.

## Accounts and privacy

- Invitation-only: an admin issues a single-use link; the recipient picks the
  password and verifies the mailbox.
- Personal collections are private and non-transferable. Shared collections have
  owners and members; members remove only their own contributions.
- App admins manage accounts, invitations, shared-collection succession and the
  model connection. They can restore access to an account that lost both its
  password and mailbox — deliberately, Jellyfin style — but that is identity
  recovery, not permission to read other people's collections.
- Sensitive admin actions need recent reauthentication. Password retries use
  allauth's account reauthentication and failed-login limits.
- Accounts are disabled, never deleted; disabling revokes sessions and keeps
  content, memberships and ownership.
- Tailscale is transport only. LAN and remote requests get identical checks, and
  forwarded/identity headers never authenticate anyone.

## Migrating an existing `library.json`

From the running app, an old `library.json` goes through the same import dialog
as a TikTok export; entries land in the collection you pick. Media referenced
by the old library is retained only when the old `cache/` directory was copied
to `<data dir>/import-cache` on the server first; otherwise the manifest lists
what was missing.

For offline or bulk imports there is the management command:

```
python clipshelf.py import_library --user you@example.com --input library.json --cache cache --dry-run
python clipshelf.py import_library --user you@example.com --input library.json --cache cache
```

The dry run reports a manifest digest and counts, and imports nothing.
The real run imports into that account's Personal collection
only; the original files are never modified and a repeated import is a no-op.

## Layout

| path | what |
|---|---|
| `clipshelf/models.py`, `services.py` | collections, captures, jobs, entries/contributions, assets |
| `clipshelf/views.py`, `urls.py` | JSON API and app shell |
| `clipshelf/accounts.py`, `admin_views.py` | invitation admission, native sessions, admin endpoints |
| `clipshelf/network.py`, `acquisition.py`, `interpretation.py` | bounded fetching, extractors, model calls |
| `clipshelf/worker.py`, `management/commands/` | job coordinator and operator commands |
| `clipshelf/templates/`, `static/` | web UI |
| `android/` | native client |
| `research/` | the plan and the research behind it |

Tests: `python clipshelf.py test` plus `python test_clipshelf.py` and
`python test_sources.py` (both use project dependencies and avoid external network).
For the web DOM checks, run `node test_dom_events.mjs` and `node test_web_assets.mjs`.

### Mutation testing

`python mutate.py` checks that the tests actually pin the domain logic: it
generates single-site mutants (operator swaps, constant tweaks) of the target
modules and runs the normal offline test suite once per mutant. A mutant the
suite still passes is a survived mutant — a behavior no test guards.

- Stdlib only (Python 3.13, `ast` + `unittest`), no new dependencies; the
  mutation tool never ships in the runtime image.
- Safety: mutants run in a workspace under `TEMP/clipshelf-mutation/` that
  holds only the git-tracked files (gitignored personal data, caches and
  keystores are never copied), with a per-mutant throwaway
  `CLIPSHELF_DATA_DIR`; the checkout is never modified, mutant runs execute
  only the offline test suite (no network, no other commands), and nothing
  the harness creates is deleted.
- Scope: `DEFAULT_TARGETS` in `mutate.py` — the pure-logic domain modules.
  For a change-sized run use `python mutate.py --diff origin/main`; view and
  command modules join the scope the same way. Tests, migrations and wiring
  are never mutated (the suite is the oracle).
- Gate: use the project Python 3.13 virtual environment and run
  `python mutate.py --max-mutants 116` from the repository root. It runs the
  full CI test suite for each of exactly 116 evenly spaced sites across
  `DEFAULT_TARGETS`, in listed file/AST order. For site count `S`, the chosen
  indices are `floor(i*S/116)` for `i = 0..115`; use the same commit and target
  list to repeat a score. The `DEFAULT_FAIL_UNDER` floor is 72%; never lower it.
- The old stride sampler did **not** select the requested count: 950 sites with
  `--max-mutants 116` gave stride 9 and just 106 mutants (73/106 = 68.9% in
  the release review). The earlier 94/116 = 81.0% record cannot come from
  the 950-site tree with that sampler; an older 921-site tree would select 116
  at stride 8. The scores cannot be compared as a ratchet.
- On the 950-site task snapshot, the sample killed 90/116 = 77.6%. Two
  mutants failed on unrelated Windows restore file-lock errors; discounting
  them yields 88/116 = 75.9%. The rebased 946-site scope killed 88/116 =
  75.9%. Earlier runs had up to seven false kills, so the 72% floor leaves a
  margin. Inspect `KILLED` diagnostics before raising it. A timeout counts as
  a kill, not evidence that a test caught the mutant.
- `python mutate.py self-check` verifies the harness. A sample is a practical
  gate, not the full-scope score; use `python mutate.py` for all sites.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party code is inventoried in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and gated by
`check_licenses.py` against `license_inventory.toml`.
