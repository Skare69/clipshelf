# Configuration and hardening

Nothing is required beyond image, UID/GID, data dir and port. The secret key is
generated into the data directory on first start. Once SMTP is configured, set
`CLIPSHELF_ORIGIN` to the URL users open (e.g. `http://nas.example:8000`):
account mail never builds links from the request's Host header, so without it
confirmation, reset and signup mails carry path-only links, and
`clipshelf.py check` warns. `CLIPSHELF_ALLOWED_HOSTS`
defaults to `*` because the expected deployment is a LAN with remote access
behind a VPN. API CSRF protection is off by default, like Jellyfin and the
*arr services; the browser forms (setup wizard, sign-in, sign-up, sign-out)
always check their CSRF token, and other cookie
mutations stay protected by `SameSite=Lax` sessions
regardless. Setting `CLIPSHELF_CSRF=1` turns CSRF verification on and then
requires `CLIPSHELF_ORIGIN` or a concrete `CLIPSHELF_ALLOWED_HOSTS`. Setting
Setting `CLIPSHELF_ORIGIN` to an `https://` URL additionally enables the HTTPS
redirect, HSTS and secure cookies — set it only when a TLS ingress actually
terminates in front of the app. Behind such an ingress, set it before you open
the setup wizard. Without it, the wizard's CSRF check sees an `https://` Origin
on a plain-HTTP request and answers 403. Wildcard hosts permit DNS rebinding;
narrow them by setting `CLIPSHELF_ALLOWED_HOSTS`.

## Import ceiling

`CLIPSHELF_MAX_IMPORT_BYTES` sets the import upload ceiling in bytes. The
default is `33554432` (32 MiB); `deploy/clipshelf.env.example` lists the
variable. The web server refuses a request body larger than this ceiling plus
1 MiB of multipart overhead with `413` before the request reaches Django, so
one request cannot buffer more than that bound into a temp file. Raise the
ceiling only together with the memory headroom for one import (see
[operations.md](operations.md)).

## Followed sources

Followed creator profiles are checked by the worker on a schedule. Two
instance-wide settings hold the schedule; the admin API
(`api/admin/follows-settings`, GET and POST with reauthentication) reads and
writes them:

- `poll_seconds` — seconds between checks per enabled source. Default `21600`
  (6 hours), range 300–86400.
- `backfill_cap` — posts queued by the first check of a new follow (newest
  first). Default `50`, range 1–200.

A failed check backs off with the same schedule as capture jobs, and a paused
source is never checked. See [how-it-works.md](how-it-works.md#followed-sources).

## Update check

The footer shows a link to a newer release when one is available. The check is
opt-in: an unset `CLIPSHELF_UPDATE_CHECK` makes no request, and
`CLIPSHELF_UPDATE_CHECK=1` lets the server ask the GitHub API
(`api.github.com/repos/Skare69/clipshelf/releases/latest`) in the background.
The server never blocks a page for the check. A good answer stays for one
hour and stops new requests for that hour. A failed check hides the notice,
and the server waits 60 seconds before it asks again, so continued failures
can cause several requests in one hour. Prereleases and `dev` builds never
show the notice, and the server never asks about them. Set
`CLIPSHELF_UPDATE_CHECK=0` to keep the check off explicitly.
