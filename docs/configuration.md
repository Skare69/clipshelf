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
`CLIPSHELF_ORIGIN` to an `https://` URL additionally enables the HTTPS
redirect, HSTS and secure cookies — set it only when a TLS ingress actually
terminates in front of the app. Behind such an ingress, set it before you open
the setup wizard. Without it, the wizard's CSRF check sees an `https://` Origin
on a plain-HTTP request and answers 403. Wildcard hosts permit DNS rebinding;
narrow them by setting `CLIPSHELF_ALLOWED_HOSTS`.
