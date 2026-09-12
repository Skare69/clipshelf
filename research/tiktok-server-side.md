# TikTok acquisition, server-side

Research 2026-08-26 (browser tier) / re-verified and **substantially corrected
2026-09-11** for the headless Docker server. Companion to `server-plan.md`;
Android/Tailscale mechanics in `android-sharing.md`.

Evidence markers: **[SRC]** read in extractor source · **[LIVE]** probed
2026-09-11 over unauthenticated HTTP, or a linked run log · **[EXP]** expected,
unverified.

## Correction notice

The first draft of this file (2026-09-11, earlier the same day) asserted that
server-side acquisition is "an arms race, not viable" and that yt-dlp is the
byte-fetcher for shared links. Both are wrong, and one supporting citation did
not say what it was cited for. Corrections in §5. The practical consequence is
large: **the tool that matters most for clipshelf is gallery-dl, not yt-dlp**,
because clipshelf's material is largely photo carousels and yt-dlp does not
extract carousel images at all.

## 1. What the browser exporter actually buys

`tiktok-extract.js` runs in the user's logged-in tiktok.com tab, reads
`__UNIVERSAL_DATA_FOR_REHYDRATION__`, and fetches slide/video bytes in-session
[SRC]. Its one irreplaceable advantage is **the authenticated session**:
private, friends-only, and follower-gated posts. It is *not* a general anti-bot
advantage — see §2.

Not to be confused with TikTok's official **"Download your data"** export
(Settings → Account): an asynchronous request-and-wait package of account
activity as JSON/TXT that contains **links and metadata, no media files**, and
has no programmatic API — so it cannot serve server-side ingestion.
([TikTok support](https://support.tiktok.com/en/account-and-privacy/personalized-ads-and-data/requesting-your-data))

## 2. The WAF challenge is solved in pure Python, no browser

TikTok serves a silent JS WAF challenge ("Please wait…" page, cookies
`_wafchallengeid`/`waforiginalreid`). **Both** maintained extractors solve it
natively — a SHA-256 proof-of-work, no JS runtime, no headless browser:

- yt-dlp `_solve_challenge_and_set_cookies` [SRC:
  [tiktok.py](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py)],
  shipped in nightlies from ~2026-01-25, fixing
  [#15418](https://github.com/yt-dlp/yt-dlp/issues/15418) (run logs there show
  it working with "JS runtimes: none"). Webpage fetches use
  `impersonate=True` (curl_cffi TLS impersonation).
- gallery-dl `_solve_challenge` [SRC:
  [tiktok.py](https://github.com/mikf/gallery-dl/blob/master/gallery_dl/extractor/tiktok.py)],
  same scheme, translated in [PR #8850](https://github.com/mikf/gallery-dl/pull/8850).

So the arms race exists, but it is **fought upstream by two actively patched
projects**. clipshelf's job is to pin fresh versions and re-invoke them — never
to re-implement signing, and not to reach for a headless browser first.

## 3. Capability matrix

| Case | yt-dlp | gallery-dl | oEmbed | Browser exporter |
|---|---|---|---|---|
| Public `/video/` bytes | **yes** [SRC/LIVE] | yes (delegates via `video:"ytdl"`) [SRC] | metadata only [LIVE] | yes |
| **Photo carousel slides** | **no** — `/photo/` is "Unsupported URL"; via `/video/` rewrite it yields **audio only**. Photos are explicitly out of scope upstream ([#10034](https://github.com/yt-dlp/yt-dlp/issues/10034) NOT_PLANNED, dup of #6168/#6217/#8360/#9990) | **yes** — `imagePost.images[].imageURL`, one file per slide, plus audio and optional subtitles [SRC] | 400 [LIVE ×2] | yes |
| `vm.`/`vt.` shortlinks | yes — plain HTTP with UA `facebookexternalhit/1.1` [SRC] | yes, same trick [SRC] | **400 — resolve first** [LIVE] | browser-native |
| Spoken content | TikTok's own ASR captions as srt/vtt when present (`--write-subs`) [SRC] | same (`subtitles=true`) [SRC] | no | no (desc only) |
| Private post / account | `statusCode` 10216/10222 → login required; works with `--cookies` [SRC] | same, cookies supported [SRC] | [EXP] 400 | **yes, if the tab's account has access** |
| Removed / region-locked | 10204 → "IP blocked"; region via 10204 [SRC] | 10204 → "not available"; 10231 → "region locked" [SRC] | 400 [LIVE] | records an error field |

**oEmbed, live-verified** (`GET https://www.tiktok.com/oembed?url=…`, no auth,
[docs](https://developers.tiktok.com/docs/en/embed-videos)): canonical
`/video/` URL → 200 with title, author, embed HTML, one thumbnail. It returns
**no slide sets, no bytes, no transcription**, and 400s on shortlinks and (2/2
probes) photo posts. `thumbnail_url` is CDN-signed with `x-expires` — cache the
bytes, never store the URL.

## 4. Acquisition order (proposal)

**Deployment policy approved 2026-09-12:** server acquisition uses no TikTok
account login and accepts/stores no TikTok login-session cookies. Anonymous
WAF/challenge cookies remain permitted. Browser export/import is the logged-in
fallback; the account session stays in the user's browser. Login-required or
blocked posts remain saved and visibly flagged for retry or browser import,
even if some metadata was acquired. The matrix above describes extractor
capabilities, not permission to deploy authenticated acquisition.

1. **`resolve()`** — canonicalize `vm.`/`vt.`/`/t/<id>` with plain HTTP, UA
   `facebookexternalhit/1.1`. clipshelf already resolves; only the UA changes.
2. **oEmbed** on the canonical URL, tolerate 400. Gives a title/author floor so
   an entry is never contentless, even if every download fails.
3. **gallery-dl** for `/photo/` carousels — the only maintained tool that
   returns the full slide set, which is clipshelf's dominant material.
4. **yt-dlp** for `/video/` bytes. Kept separate from step 3 so the two fail
   independently (they *can* collapse into gallery-dl with `video:"ytdl"`).
5. **Browser exporter + `tiktok.json` upload** stays as the authenticated and
   bulk-backfill tier. The server cannot replicate its session advantage
   without taking custody of the user's TikTok cookies.
6. **Playwright: not a candidate.** Bounded last resort only if both extractors
   break for weeks. +500 MB image, cookie custody, unverified benefit over the
   native solvers.

Captions: prefer TikTok's own ASR srt when present (free, both tools). Real
transcription is local ASR — a separate feature, not part of acquisition.

Docker/NAS feasibility: both tools are plain `pip install` (yt-dlp wants the
`curl_cffi` extra for impersonation), single-binary CLIs, headless-safe,
runnable as an unprivileged user writing to the cache volume. Install both
**via pip at image build** — gallery-dl [#8466](https://github.com/mikf/gallery-dl/issues/8466)
was a stale *bundled* yt-dlp, not a TikTok block. Expect periodic breakage
windows and rebuild; #15418 was fixed in nightly within ~4 weeks.

## 5. Claims deleted from the first draft

1. **"ytdlnis #1039 documents automated WebView logins being blocked outright"**
   — false. [#1039](https://github.com/deniscerri/ytdlnis/issues/1039) is an
   Android WebView UX bug (`snssdk1233://` scheme redirect), closed fixed
   2026-06-21. The "Playwright trips fingerprint challenges regularly" claim
   had **no valid citation** and is withdrawn.
2. **"Datacenter IPs get blocked faster"** — the premise collapses here: a home
   NAS on a consumer ISP *is* a residential IP. The cited article was about
   YouTube's sign-in wall, not TikTok. Primary evidence shows challenge serving
   is heuristic and regional, "not tied to IP [or] rate limiting"
   ([#15418 comment](https://github.com/yt-dlp/yt-dlp/issues/15418#issuecomment-3782113215)).
3. **Wrong error string** — the documented breakage was `Unable to extract
   webpage video data`; `Unexpected response from webpage request` is the
   current challenge-solver string. Waves-and-nightly-fixes was right, details
   were not.
4. **"Server-side scrape replication is not viable"** — overstated; it is
   precisely what both extractors do daily. What is *not* replicable
   server-side is the authenticated session, i.e. §1.

## 6. Per-entry result state (proposal)

Acquisition records an outcome, not a boolean:
`{media: video|slides(n)|audio|none, metadata: oembed|og|none, captions: asr|none,
tier: server|browser-export, failure: none|private|unavailable|blocked|challenge}`.

Partial results remain useful evidence: slides without audio allow visual
interpretation; an audio-only slideshow is flagged "slides missing". Metadata
does not clear a media-acquisition failure or imply full interpretation. If
metadata acquisition also fails, preserve a URL-only capture with an honest
warning. Retry and browser export/import remain available; do not invent captions
or declare success merely because some metadata exists.

## 7. Runtime uncertainties (must be measured, not assumed)

- oEmbed against a confirmed-live photo post and a private post (both 400s
  above are confounded by possible deletion).
- oEmbed throttling thresholds under burst — no published limit.
- `statusCode` 10204 semantics: yt-dlp reads it as IP-blocked, gallery-dl as
  removed, from the same payload. Mapping `failure` correctly needs live
  observation.
- Durability of the PoW solvers across TikTok rotations.
- Per-post availability of slideshow audio (`music.playUrl`).
