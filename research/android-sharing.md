# Sharing links from an Android phone into a self-hosted web app

Research for clipshelf (LAN/Tailscale web app with `POST /add` accepting raw text, URLs extracted). Researched 2026-08-26; primary sources cited per claim.

> **Status 2026-09-12:** this is a historical survey, not the current client plan.
> The approved workflow requires durable offline capture and delivery without
> reopening Clipshelf, with online-only library browsing. The
> [consolidated plan](server-plan.md) proposes a native Android client with a
> SQLite outbox and WorkManager. The PWA-only recommendation and insecure-origin
> workarounds below do not define the shipping authentication/capture contract.

## 1. Web Share Target API (PWA, Chrome on Android)

**What it is.** A W3C spec ([Web Share Target](https://w3c.github.io/web-share-target/)) adding a `share_target` member to the Web App Manifest that registers the web app in the OS share sheet. Chrome implements it on Android (Chrome 76+) and desktop (Chrome 89+). Sources: [Chrome — Web Share Target](https://developer.chrome.com/docs/capabilities/web-apis/web-share-target) (page last updated 2019-11-08), [MDN — manifest `share_target`](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest/share_target) (marked *experimental*). Official samples: Squoosh, Scrapbook PWA (linked from the Chrome doc).

**Mechanics** (Chrome doc + W3C processing rules):
- Manifest block: `share_target` object. Required: `action` (URL opened when the target is chosen) and `params` (non-empty object naming which of `title` / `text` / `url` to send). Optional: `method` — default **GET**; `enctype` — POST only, `application/x-www-form-urlencoded` (default) or `multipart/form-data`. W3C rules: `action` must be within the manifest's scope, and its origin must be "potentially trustworthy" (i.e. a secure context; see [W3C Secure Contexts](https://www.w3.org/TR/secure-contexts/)).
- **GET**: the browser navigates to the action URL with the params as a query string; parse via `new URLSearchParams(location.search)`. `enctype` is ignored for GET (Chrome doc).
- **POST**: the body is POSTed to the action URL. On Android the POST is intercepted by the PWA's **service worker** fetch handler (so a SW is required for the POST path); the recommended pattern is to answer with a **303 See Other** redirect into the app (Chrome doc, MDN).
- Chrome-only extension: `files` in `params` shares real files; requires POST + `multipart/form-data` + a `files` param entry (Chrome doc).
- **Fields actually sent / the gotcha:** on **Android the `url` member arrives empty** (Chrome bug [crbug 789379](https://crbug.com/789379)) — the shared URL typically lands in `text` (sometimes `title`). For clipshelf: extract the URL from `text`. With `x-www-form-urlencoded`, spaces encode as `+`, not `%20` (W3C).
- **Registration:** the spec leaves registration to the UA and permits registering without install; **Chrome requires the app to be installed** (added to home screen) before it appears in the share sheet (Chrome doc: "your web app must be installed before it appears as a share target"; MDN agrees).
- **Share-sheet appearance:** entry shows the PWA icon + `name`; on Android you can long-press the entry → **Pin** to keep it at the top (documented in linkding's official how-to, §6).

**Requirements / limits**
- Installability: valid manifest (name, `display` standalone/fullscreen/minimal-ui, 192 + 512 icons, `start_url`), a **secure (HTTPS) origin**, and user engagement. Current criteria: [web.dev — install-criteria](https://web.dev/articles/install-criteria) (updated 2024-09-19). Note the service worker is no longer listed as an installability requirement (removed with Universal Install, Chrome 124, 2024) — but a SW is still needed for the POST path above.
- HTTPS is a hard requirement on plain origins → §2 workarounds.
- **Browser support (2026):** Chrome Android/desktop ✓; Microsoft Edge (Chromium) ✓ — [Microsoft Edge PWA share docs](https://learn.microsoft.com/en-us/microsoft-edge/progressive-web-apps/how-to/share); Samsung Internet (Chromium-based) ✓ per Samsung developer blog/community reports; Firefox ✗ (no Web Share Target); Safari ✗ ([WebKit bug 194593](https://bugs.webkit.org/show_bug.cgi?id=194593) open). MDN's compatibility data lists the feature overall as experimental.

**Sources:** https://developer.chrome.com/docs/capabilities/web-apis/web-share-target · https://w3c.github.io/web-share-target/ · https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest/share_target · https://web.dev/articles/install-criteria · https://learn.microsoft.com/en-us/microsoft-edge/progressive-web-apps/how-to/share · https://www.w3.org/TR/secure-contexts/

## 2. PWA installability without public HTTPS

**Why it matters.** Installability requires a secure context, so `http://192.168.x.x:8000` won't install — and a non-trustworthy origin can't host a share target either (W3C "potentially trustworthy" origin rule; [secure-contexts](https://www.w3.org/TR/secure-contexts/)). Chromium gates powerful features on secure contexts: [chromium.org — deprecating powerful features on insecure origins](https://www.chromium.org/Home/chromium-security/deprecating-powerful-features-on-insecure-origins/).

**Option A — `unsafely-treat-insecure-origin-as-secure` Chrome flag (per-device)**
- Set at `chrome://flags/#unsafely-treat-insecure-origin-as-secure` in the phone's Chrome — per device / per browser profile. Enter the exact origin (`http://192.168.1.50:8000`, no wildcards), enable, relaunch. The origin then counts as secure: the PWA install prompt appears and secure-context features work.
- Equivalent command-line switch `--unsafely-treat-insecure-origin-as-secure=<comma-separated origins>`; documented via the Chrome Enterprise policy page: [chromeenterprise.google — UnsafelyTreatInsecureOriginAsSecure](https://chromeenterprise.google/policies/unsafely-treat-insecure-origin-as-secure/) (it notes the *enterprise policy* was deprecated in M69 in favor of `OverrideSecurityRestrictionsOnInsecureOrigin`; the chrome://flags entry itself remains).
- Caveats: every phone/profile must be configured; Chrome shows a persistent "unsupported command-line flag" warning bar; the flag is explicitly unsafe (disables MitM protection for that origin) — acceptable on a trusted LAN/VPN.

**Option B — `tailscale serve` (valid HTTPS for the tailnet, zero cert work)**
- [Tailscale — Serve KB](https://tailscale.com/kb/312/serve) (page verified 2026-01-20): with **HTTPS certificates enabled** on the tailnet (admin console → DNS → MagicDNS + "Enable HTTPS"; node names and the tailnet name are published to the public Certificate Transparency log), `tailscale serve 3000` automatically provisions a **Let's Encrypt** certificate and serves `https://<node-name>.<tailnet>.ts.net`, proxying to `localhost:3000`.
- The phone joins the tailnet and gets a genuinely valid HTTPS secure context: no flag, no self-signed cert, no public exposure (public exposure would be `tailscale funnel`).
- Bonus: `tailscale serve` injects identity headers (`Tailscale-User-Login` etc.) — usable to authenticate `POST /add`.
- CLI syntax changed in Tailscale 1.52 (e.g. bare `tailscale serve 3000`). Use the `*.ts.net` MagicDNS name on the phone, not the LAN IP — that's the name the cert is valid for.

**Sources:** https://tailscale.com/kb/312/serve · https://chromeenterprise.google/policies/unsafely-treat-insecure-origin-as-secure/ · https://www.chromium.org/Home/chromium-security/deprecating-powerful-features-on-insecure-origins/ · https://www.w3.org/TR/secure-contexts/ · https://web.dev/articles/install-criteria

## 3. HTTP Shortcuts (Waboodoo)

**What it is.** Open-source Android app (Kotlin, MIT) that fires arbitrary HTTP requests from home-screen shortcuts, widgets, quick-settings tiles, and the share sheet: [GitHub — Waboodoo/HTTP-Shortcuts](https://github.com/Waboodoo/HTTP-Shortcuts) (≈1.8k stars, 12 open issues, active releases/docs pipeline as of 2026-08-26). Docs: [http-shortcuts.rmy.ch](https://http-shortcuts.rmy.ch/). Distribution: [Google Play](https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts), [F-Droid](https://f-droid.org/en/packages/ch.rmy.android.http_shortcuts/), APKs from GitHub Releases.

**How it works mechanically**
- A shortcut = an HTTP request: GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS/TRACE, custom headers, basic/digest/bearer auth, custom request body (text/file), file uploads, self-signed certificates, HTTP/SOCKS proxies, TLS 1.3 (README).
- **Share-sheet registration:** a shortcut can be configured to **receive values from the share dialog** (shared text and/or URL injected into a variable); on Android 11+ it can also be a **Direct Share target**, so it shows directly in the share sheet (docs: variables + shortcuts pages). The shared value is then used anywhere: URL, headers, request body — e.g. `POST /add` with body `text=${shared}`.
- Optional JavaScript pre/post-processing of the shared value (scripting docs).
- The share-sheet entry appears as the app's "Send to…" item (display name limited by Android).
- Proven pattern: **linkding ships an importable shortcut JSON** for exactly this (§6).

**Setup / limits.** Low effort: one shortcut (or imported JSON) + instance-URL/API-key variables. Works over plain HTTP on the LAN (no HTTPS required); self-signed TLS supported if you add it. Cost: a per-phone app install and config; server needs only the endpoint.

**Sources:** https://github.com/Waboodoo/HTTP-Shortcuts · https://http-shortcuts.rmy.ch/ (variables: receiving share-dialog values; scripting; shortcuts) · https://play.google.com/store/apps/details?id=ch.rmy.android.http_shortcuts · https://f-droid.org/en/packages/ch.rmy.android.http_shortcuts/

## 4. Automation apps (Tasker, MacroDroid, Automate)

All three can be share-sheet targets and fire an HTTP POST containing the shared text/URL.

**Tasker** — [Received Share user guide](https://tasker.joaoapps.com/userguide/en/help/sh_received_share.html): a **Received Share** event fires a task when another app shares to Tasker (can be a Direct Share target shown directly in the share sheet); filter by source app; all shared extras available as JSON in `%rs_all_extras`. The task then issues an HTTP request via Tasker's HTTP plugin. Paid app + paid plugin; most flexible, steepest learning curve.

**MacroDroid** — official [wiki — Triggers](https://wiki.macrodroid.com/wiki/index.php?title=Triggers): **"Text Shared to MacroDroid"** and **"File Shared to MacroDroid"** triggers; the shared content is exposed as magic-text variables usable by any action, e.g. an HTTP Request action that POSTs to the server. Free core app. (Also has "HTTP Server Request" and "Webhook (URL)" triggers, so it can *receive* webhooks too.)

**Automate (LlamaLab)** — [Content shared block](https://llamalab.com/automate/doc/block/content_shared.html) ([Play Store](https://play.google.com/store/apps/details?id=com.llamalab.automate)): a **"Content shared"** block pauses a running flow until content is shared to the "Automate flow" (can be a Direct Share target); receives Content URI, MIME type, text, and subject, with a MIME-type filter; the flow then performs an HTTP request. Caveat: the flow must be running in the background — it's a block, not a standalone trigger. Free.

**Setup complexity:** MacroDroid lowest (one macro: share trigger + HTTP action) → Automate (one background flow) → Tasker (task + HTTP plugin, both paid).

**Sources:** https://tasker.joaoapps.com/userguide/en/help/sh_received_share.html · https://wiki.macrodroid.com/wiki/index.php?title=Triggers · https://llamalab.com/automate/doc/block/content_shared.html

## 5. Termux (`termux-url-opener` + curl)

**Mechanism** (primary source: [Termux wiki — Intents and Hooks](https://wiki.termux.com/wiki/Intents_and_Hooks)): Termux supports **hooks** — for certain intents it executes an executable script from the Termux home directory. When a URL is shared to the Termux app, Termux runs **`~/bin/termux-url-opener`**, passing the URL as the first argument. (Correction to the common `~/.termux/termux-url-opener` recollection: the wiki puts the script in `~/bin/`, i.e. `/data/data/com.termux/files/home/bin/`.)

So: install Termux (F-Droid/GitHub build of `com.termux`; the Play Store build is deprecated — [Termux wiki](https://wiki.termux.com/wiki/)) and drop a script at `~/bin/termux-url-opener`:

```sh
#!/data/data/com.termux/files/usr/bin/bash
curl -s -X POST --data-urlencode "text=$1" http://192.168.1.50:8000/add
```

Share any URL to the Termux entry → curl fires. Plain-HTTP LAN works; no HTTPS, no cloud, no app-config UI. Lowest effort; least "native" (silent, no confirmation).

**Sources:** https://wiki.termux.com/wiki/Intents_and_Hooks · https://wiki.termux.com/wiki/ · https://f-droid.org/packages/com.termux/

## 6. Prior art in self-hosted link managers

**linkding** (Django; [github.com/sissbruecker/linkding](https://github.com/sissbruecker/linkding)) — the most complete Android story. The official [how-to docs](https://linkding.link/how-to/) document **three** routes:
1. **PWA + native share sheet:** linkding's how-to documents installing its PWA and pinning it in Android's share sheet. This documents the workflow; it is not proof that a particular repository checkout contains a `share_target` manifest.
2. **Chrome bookmarklet workaround:** bookmark `javascript:window.open(\`http://<instance>/bookmarks/new?url=${encodeURIComponent(window.location)}&auto_close\`)`, then type the bookmark's name into Chrome's address bar. Docs cite [paul.kinlan.me/use-bookmarklets-on-chrome-on-android](https://paul.kinlan.me/use-bookmarklets-on-chrome-on-android/).
3. **HTTP Shortcuts:** import [linkding_shortcut.json](https://raw.githubusercontent.com/sissbruecker/linkding/master/docs/src/assets/linkding_shortcut.json) from the repo; set `linkding_instance` + `linkding_api_key` variables; a "Send to…" item appears in the share sheet that POSTs to the REST API.

Plus a [REST API](https://linkding.link/api/) for any other client. README: installable as a PWA, browser extensions, bookmarklets.

**Linkwarden** ([github.com/linkwarden/linkwarden](https://github.com/linkwarden/linkwarden); repo moved from `theshyft/linkwarden`) — ships **official native iOS + Android apps** (README: "Native iOS and android mobile apps"; announced in the [mobile-app release post](https://linkwarden.app/blog/releases/mobile-app), late 2025). The PWA itself has **no** share target — it's an open feature request ([issue #323](https://github.com/linkwarden/linkwarden/issues/323)). Also has a browser extension and community clients (e.g. [LinkGuardian](https://github.com/Elbullazul/LinkGuardian), an Android client).

**wallabag** (PHP; [github.com/wallabag/wallabag](https://github.com/wallabag/wallabag)) — **official Android app**: [wallabag/android-app](https://github.com/wallabag/android-app) ("Android application for reading articles saved in wallabag. You can also easily add new articles"). Adding happens inside the app, not via a generic share-sheet entry into the web app.

**Shiori** (Go, single binary; [github.com/go-shiori/shiori](https://github.com/go-shiori/shiori)) — CLI + web app; ingest via CLI, web UI, [REST API](https://github.com/go-shiori/shiori/blob/master/docs/index.md), and a **[BETA] Firefox/Chrome web extension** ([go-shiori/shiori-web-ext](https://github.com/go-shiori/shiori-web-ext)). No official Android app; Android users pair the API with an external sharer (§3/§4).

**Readeck** (Go + HTMX web app; [codeberg.org/readeck/readeck](https://codeberg.org/readeck/readeck), [readeck.org](https://readeck.org)) — web app + REST API. No `share_target`/PWA manifest found in the frontend source (`web/`, `web/ui/`) and no dedicated Android app in the official docs as of 2026-08-26; Android ingestion is web-only or API + external sharer.

## 7. Device-to-device without cloud (KDE Connect, LocalSend)

**KDE Connect** ([kdeconnect.kde.org](https://kdeconnect.kde.org/)) — open-source device-pairing suite: share files and **links** between devices, open a link on another device, Run Commands, notifications, remote control; **Plasma Browser Integration** adds a KDE Connect entry to the Firefox/Chrome context menu (a web-page share path on desktop). Pairing is direct over the LAN — no cloud. To push into a home *server*, run the KDE Connect daemon on the server (Linux) and let it act as the receiving device: still device-to-device, not a built-in HTTP endpoint.

**LocalSend** ([localsend.org](https://localsend.org), [github.com/localsend/LocalSend](https://github.com/localsend/LocalSend)) — cross-platform open-source app for sending files/links/text **locally only** (LAN; no internet connection or third-party server required); cross-platform; includes a REST API with optional HTTPS (README). Also device-to-device: the receiving end must run the LocalSend client (or use its web/REST receiver) on the server.

Both are good for "URL/clipboard → another device" but add a second moving part vs. a direct POST to the app.

## Comparison

| Option | Setup effort | Feels native? | Needs HTTPS? | Needs server changes? | Cloud-free? |
|---|---|---|---|---|---|
| **PWA share_target** (Chrome/Edge) | Medium: manifest + SW + one-time install | **Yes** — native share-sheet entry, pinnable | Yes (secure context) | Yes — `share_target` manifest + the POST/GET endpoint (`/add`) | Yes |
| **PWA + `tailscale serve`** | Low-medium: enable tailnet HTTPS certs | Yes | Yes — auto-provisioned valid Let's Encrypt on `*.ts.net` | Same as PWA | Yes (tailnet) |
| **PWA + `unsafely-…-as-secure` flag** | Low per device, fiddly: flag set on every phone | Yes (with warning bar) | No — flag fakes a secure context | Same as PWA | Yes |
| **HTTP Shortcuts** | Low: one shortcut / import a JSON | Partial — "Send to…" share-sheet item | No (self-signed TLS supported) | Endpoint only (+ optional API key) | Yes |
| **Tasker** | Medium: task + HTTP plugin (paid) | Partial (Direct Share target) | No | Endpoint only | Yes |
| **MacroDroid** | Low: share trigger + HTTP action | Partial | No | Endpoint only | Yes |
| **Automate (LlamaLab)** | Low-medium: background flow must run | Partial | No | Endpoint only | Yes |
| **Termux hook + curl** | Low: one shell script | No (silent, no UI) | No | Endpoint only | Yes |
| **KDE Connect** | Medium: daemon on both ends | No (separate app) | No | Server-side daemon | Yes |
| **LocalSend** | Low-medium: client on both ends | No (separate app) | No | Server-side receiver (client/web/REST) | Yes |

**Earlier recommendation, superseded for the current requirements.** A PWA
`share_target` remains an online-capture option, but installability does not prove
durable unattended delivery. The revised Clipshelf plan uses native Android
capture, an app-private outbox, and WorkManager; library browsing remains online.
Use the official [share receiver](https://developer.android.com/develop/ui/compose/sharing/receive)
and [persistent work](https://developer.android.com/develop/background-work/background-tasks/persistent)
contracts, and prove delivery on the real phone. Production LAN and remote login
both require the approved HTTPS/authentication setup, not insecure-origin flags.
