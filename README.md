# clipshelf

[![tests](https://github.com/Skare69/clipshelf/actions/workflows/ci.yml/badge.svg)](https://github.com/Skare69/clipshelf/actions/workflows/ci.yml)

Share a link from your phone; your own server saves it, fetches the material, and
an AI endpoint you control turns it into a curated library of repos, prompts and
guides. Self-hosted, account-based, no third-party service in the loop.

![clipshelf](screenshot.jpg)

## What it is

| Piece | What it does |
|---|---|
| Django app (`clipshelf/`) | accounts, collections, capture receipts, library/search, source viewing, admin |
| Worker (`clipshelf.py worker`) | acquisition (pages, TikTok video/carousels), interpretation, retries |
| Android app (`android/`) | share-sheet capture with a durable outbox, background delivery, online browsing |
| Container (`Dockerfile`, `compose.yaml`) | one image, web + worker roles, SQLite and retained media on one data volume |
| `tiktok-extract.js` | browser export for login-required posts; the server never takes TikTok cookies |

Data lives in one directory (`CLIPSHELF_DATA_DIR`): `db.sqlite3` plus `assets/`
(retained pages, images, video). Nothing leaves it except the outbound fetches
the worker makes and the calls to the model endpoint you configure.

## Run it locally

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data python clipshelf.py migrate
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data python clipshelf.py bootstrap_admin --email you@example.com
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data python clipshelf.py serve 127.0.0.1:8000
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data python clipshelf.py worker
```

`bootstrap_admin` prints a single-use link to set the first password. In
development mail goes to the console; production needs a real SMTP relay
(`CLIPSHELF_SMTP_*`) for invitations and password recovery.

## Deploy on the NAS

Published image (linux/amd64 + linux/arm64), built and verified by CI:
`ghcr.io/skare69/clipshelf:0.1.0`. The Android APK is built and signed by the
`android-release` workflow and downloaded from its run artifacts — there is no
app store, you sideload it.

```
cp deploy/clipshelf.env.example .env      # fill in image, origin, hosts, data dir, SMTP
docker compose --profile ops run --rm migrate
docker compose up -d
docker compose --profile ops run --rm bootstrap
docker compose --profile ops run --rm verify   # SQLite WAL/synchronous check
```

The image runs as an unprivileged UID/GID you choose, mounts one local dataset,
and publishes the web port on the LAN — reaching it remotely is your tailnet's
job, not the app's. `CLIPSHELF_ALLOWED_HOSTS` must list every hostname you use.

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
4. **Browse** — Library and Inbox per collection, search, categories, source
   viewing, retry. Collections are the access boundary: everyone has a private
   Personal collection, shared collections have explicit members.

## Accounts and privacy

- Invitation-only: an admin issues a single-use link; the recipient picks the
  password and verifies the mailbox.
- Personal collections are private and non-transferable. Shared collections have
  owners and members; members remove only their own contributions.
- App admins manage accounts, invitations, shared-collection succession and the
  model connection. They can restore access to an account that lost both its
  password and mailbox — deliberately, Jellyfin style — but that is identity
  recovery, not permission to read other people's collections.
- Accounts are disabled, never deleted; disabling revokes sessions and keeps
  content, memberships and ownership.
- Tailscale is transport only. LAN and remote requests get identical checks, and
  forwarded/identity headers never authenticate anyone.

## Migrating an existing `library.json`

```
python clipshelf.py import_library --user you@example.com --input library.json --cache cache --dry-run
python clipshelf.py import_library --user you@example.com --input library.json --cache cache
```

The dry run writes a manifest (counts, per-entry field hashes, media digests) and
imports nothing. The real run imports into that account's Personal collection
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
`python test_sources.py` (both stdlib-only, no network).

## License

MIT — see [LICENSE](LICENSE).
