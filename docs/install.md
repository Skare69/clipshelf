# Install clipshelf

README.md holds the short version. This file holds the full instructions.

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
relay (`CLIPSHELF_SMTP_*`) for invitations and password recovery. Send a test
message with `python clipshelf.py sendtestemail you@example.com`.

## Deploy on the NAS

Published image (linux/amd64 + linux/arm64), built and verified by CI. Each
release tags one image three ways: `latest`, the version `X.Y.Z` and the raw
release tag `vX.Y.Z`, for example `ghcr.io/skare69/clipshelf:latest`. A manual
workflow_dispatch run publishes only `X.Y.Z` and `vX.Y.Z`, so it never moves
`latest`. The signed Android APK is attached to each
[release](https://github.com/Skare69/clipshelf/releases) — there is no app
store, you sideload it. The API can change between releases: since v0.11.0,
`/api/entries` returns the stored description as `desc` and the
interpretation summary as the new `summary` field, so a v0.10.0 app on a
v0.11.0 server shows the stored description where it showed the summary
before — update the app to v0.11.0 or later.

Android APK versions come from the release tag: `versionName` is the tag
without its `v`, and `versionCode` is `major*1_000_000 + minor*1_000 + patch`
so later tags always sort above earlier installs. An untagged commit takes the
nearest reachable `vX.Y.Z` tag; `-PversionName=X.Y.Z` overrides it, and the
build fails only when neither gives a valid version (for example a clone
without tags).

```
cp deploy/clipshelf.env.example .env      # fill in image, UID/GID, data dir, port
docker compose up -d
```

The container migrates itself on start; open the web UI and complete the setup
wizard to create the first administrator. Behind a reverse proxy that changes
the scheme or hostname (Tailscale Serve, Caddy, a NAS ingress), set
`CLIPSHELF_ORIGIN` to the proxied URL before the first visit — the setup wizard
checks the browser's Origin header and refuses the form until it matches.
`docker compose --profile ops run --rm verify` optionally re-checks the SQLite
WAL/synchronous settings. The
image runs as an unprivileged UID/GID you choose, mounts one local dataset, and
publishes the web port on the LAN — reaching it remotely is your tailnet's job,
not the app's.
