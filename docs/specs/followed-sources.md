# Followed sources

A user follows a creator profile (first TikTok) with a topic tag. Clipshelf
lists each enabled source on a schedule, remembers every post it saw, and
queues only new posts through the existing capture, acquisition and
interpretation path. The library fills with the creator's posts without one
share per post.

## Behavior

### Models (`clipshelf/models.py`, migration `0007`)

`FollowedSource` — one followed profile per row:

| field | type | notes |
|---|---|---|
| `id` | UUID pk | |
| `user` | FK user, PROTECT, `related_name="followed_sources"` | the follower |
| `platform` | TextChoices, `TIKTOK` only | YouTube or Instagram come later |
| `profile_url` | CharField(`MAX_URL_LENGTH`) | normalized TikTok profile URL |
| `tag` | CharField(100) | topic tag stamped on every entry this source publishes |
| `enabled` | BooleanField, default True | pause/resume switch |
| `full_backfill` | BooleanField, default False | first listing queues the whole window, not the cap |
| `backfill_done` | BooleanField, default False | set after the first successful listing |
| `last_check` | DateTime, null | last **successful** listing time; a failure never changes it |
| `next_check` | DateTime, null | due time; null means due now |
| `last_error` | TextField, default "" | last listing failure, max 500 chars |
| `failures` | PositiveIntegerField, default 0 | consecutive failures; drives the backoff |
| `created_at` | auto_now_add | |

Constraint: `UniqueConstraint(user, platform, profile_url)` — one follow per
profile per user. Uniqueness uses the URL exactly as `follows.validate_profile_url`
normalized it.

`SeenPost` — the never-twice ledger:

| field | type | notes |
|---|---|---|
| `id` | BigAutoField pk | |
| `source` | FK FollowedSource, CASCADE, `related_name="seen_posts"` | unfollow deletes the ledger |
| `post_id` | CharField(64) | platform post id |
| `post_url` | CharField(`MAX_URL_LENGTH`), default "" | |
| `seen_at` | auto_now_add | |

Constraint: `UniqueConstraint(source, post_id)`, name
`clipshelf_seenpost_source_post_unique`. This constraint is the double-queue
guard: two listings of the same source at the same time produce at most one
capture per post.

`Capture.follow` — nullable FK to FollowedSource, `on_delete=SET_NULL`,
`related_name="captures"`. Provenance of an auto-captured post; null for
ordinary captures. Unfollow keeps already-captured posts, jobs and entries.

### Settings

`ServerSettings` gains two fields, both operator-tunable through the admin API
(`clipshelf/admin_views.py`, same GET/POST + reauth pattern as `screening`,
route `api/admin/follows-settings`, admin namespace):

- `follow_poll_seconds`: PositiveIntegerField, default `21600` (6 hours).
- `follow_backfill_cap`: PositiveSmallIntegerField, default `50`.

A POST validates: poll seconds 300–86400, cap 1–200. The payload echoes
`{"follows_settings": {"poll_seconds", "backfill_cap"}}`.

### Listing (`clipshelf/acquisition.py`)

`list_profile_posts(url) -> list[dict]` lists a TikTok profile with yt-dlp
`--flat-playlist -J --playlist-items 1:200` (post ids and URLs only, no media
download). It runs under the `extractor_egress` proxy with the same fixed
argv, scrubbed env, 30 s socket timeout and `EXTRACTOR_TIMEOUT` bound as the
other extractors. Output: `[{"id": "<post id>", "url": "<post url>"}]`,
sorted newest first by numeric id (non-numeric ids last). Invalid input,
non-zero exit, timeout, or unreadable JSON raises `ListingError(AcquisitionError)`
whose `str` is `profile listing failed (<failure>)` with `<failure>` from the
existing `_tiktok_failure` mapping (bot challenge maps to `challenge`).
`PROFILE_LIST_MAX = 200` bounds the window.

### Queue (`clipshelf/follows.py`, new module)

`validate_profile_url(url)` — accepts only `tiktok.com` hosts (via `lib.is_tiktok`)
whose path is exactly `/@handle` (a video or photo URL of the
`/@handle/video/<id>` form is not a profile). The canonical form pins the host
to `www.tiktok.com`, drops the query, and lowercases the handle, so
`tiktok.com/@User`, `www.tiktok.com/@user` and `m.tiktok.com/@USER` are one
follow. Raises `ValueError` otherwise. Uniqueness uses this canonical form.

`create_follow(user, url, tag, full=False) -> FollowedSource` — validates the
URL and the tag (stripped, 1–100 chars), enforces the per-user/profile
uniqueness (duplicate raises `ValueError` with a stable message the view maps
to 409).

`check_source(source, say=None) -> str` — one listing cycle:

1. List the profile. On `ListingError`: write `last_error` (mapped message),
   increment `failures`, set `next_check = now + Job.backoff(failures)` (the
   existing backoff, capped at 1 h), leave `last_check` and the seen ledger
   untouched, and return the status string. Never raise.
2. On success: decide the queue candidates — all posts when the backfill is
   done or `full_backfill` is set; otherwise the newest `follow_backfill_cap`
   posts. Mark **every** listed post seen, then queue each unseen candidate
   through `services.accept_capture(user, crid, post_url, None, follow=source)`.
   The capture lands in the user's default destination (existing
   `destination_for` fallback). `crid = uuid5(NAMESPACE_URL, f"{source.id}:{post_id}")`,
   so a replay never duplicates a capture. A post whose URL fails
   `lib.norm`/`_require_url` is marked seen and skipped, never queued. An
   `accept_capture` failure rolls the post's marker and capture back and
   takes the same failure path as a listing failure (see Error cases): the
   check never raises for it, so the coordinator's schedule decides the
   retry, not an exception.
3. Success bookkeeping in one transaction: `last_check = now`, `last_error`
   cleared, `failures = 0`, `backfill_done = True`,
   `next_check = now + follow_poll_seconds`.

Per post, the seen row is inserted in its own `transaction.atomic()` block;
`IntegrityError` (a concurrent listing won) skips the queue step. This makes a
double queue impossible under the unique constraint alone, with the
deterministic `client_request_id` as the second layer.

### Worker integration (`clipshelf/worker.py`)

- `main()` claims due sources alongside imports: at most
  `SOURCE_CHECK_CONCURRENCY = 2` listings in flight, tracked per coordinator
  run so one source never lists twice at once. In-flight source ids are
  dropped from the set when their future completes.
- `_claim_sources(limit)` — `enabled=True` and (`next_check` null or past),
  oldest `next_check` first.
- `--once` drains due sources the same way before breaking.
- Tag stamping: in `_acquire_phase`, after `acquisition.acquire` returns and
  before `services.store_source`, a job whose `capture.follow` is set gets
  `source["metadata"]["tags"] = [follow.tag]` (prepended, deduplicated, capped
  at `services.TAGS_MAX`). `store_source` already carries metadata tags into
  the contribution, and `contribute_link` merges, so the topic tag survives the
  later findings write and shows in `serialize_entry` tags. The feed and the
  library tag filter both read those tags.

### API (`clipshelf/views.py`, `clipshelf/urls.py`)

All routes sit behind the existing `@api` guard (signed-in; session or mobile
token). A follow belongs to its user: another user's follow id answers 404.

| route | methods | request | response |
|---|---|---|---|
| `api/follows` | GET | — | `{"follows": [follow_json...]}` |
| `api/follows` | POST | `{"url", "tag", "full"?}` | 201 `{"follow": follow_json}`; malformed 400; duplicate 409 |
| `api/follows/<uuid>` | DELETE | — | `{"removed": true}` |
| `api/follows/<uuid>` | POST | `{"enabled": bool}` | `{"follow": follow_json}` (pause/resume) |
| `api/follows/suggest-tags` | POST | `{"url"}` | 200 `{"tags": [str]}` (1–5, nothing saved); bad URL 400; no LLM 503; listing or model failure 502 |
| `api/feed` | GET | `since` (required), `tag?`, `limit?` | `{"entries": [...], "count": n}` |

`follow_json`: `{"id", "platform", "profile_url", "tag", "enabled",
"full_backfill", "last_check" (ISO or null), "next_check", "last_error",
"created_at"}`.

`api/follows/suggest-tags` — topic tag suggestions for the follow form. It
saves nothing. The URL must pass `validate_profile_url`, else 400. With no LLM
configured the answer is 503 (`follows.LlmNotConfigured`). A listing or model
failure answers 502 with the mapped message, truncated to 300 characters
(`acquisition.ListingError`, `interpretation.InterpretationError` or
`interpretation.ConfigurationError`).

Success answers 200 `{"tags": [...]}` with 1–5 labels: the model is asked for
3–5 short labels, tags from the user's own vocabulary are reused verbatim, and
the list is capped at 5 (`SUGGEST_TAGS_MAX`). A model reply with no usable
label is a 502 too (`InterpretationError`).

The model request is one JSON data blob with:

- the profile handle (`@handle` from the normalized URL),
- at most 20 recent post titles (`SUGGEST_TITLES_MAX`),
- at most 200 tags from the user's own vocabulary (`SUGGEST_EXISTING_MAX`):
  follow tags first, then tags stored on entries in the user's accessible
  collections, each spelling once, case-insensitive, newest first.

`/api/me` also returns `llm_configured` (bool: an LLM base URL and model are
saved in admin settings, `ServerSettings.llm_configured`). The follow form
reads the field to disable the suggest action with a hint when no LLM is
configured.

`api/feed` — entries added since a timestamp for the signed-in user: entries in
the user's accessible collections with `created_at >= since`, newest first,
each serialized by `services.serialize_entry`; `tag` filters case-insensitively
on the serialized tags; `limit` defaults to 100, caps at 500; the scan window
is the newest 5000 entries since `since`. `since` must parse as an ISO
datetime and carry a UTC offset, else 400. Another user's entries never show
(collection access is the boundary).

### Web UI (`clipshelf/static/clipshelf/panels.js`, Settings panel)

A "Followed sources" section under the existing panels:

- Add form: profile URL input, topic tag input, "Include older posts on the
  first check" checkbox, **Follow** button.
- One row per source: profile URL, tag badge, state (`Enabled`/`Paused`),
  last check time (`fmtWhen`), and the last error in the error style when
  present. Row actions: Pause/Resume toggle and Remove (confirm dialog says
  already-captured posts stay).
- Same visual system as the existing panels; every state change reloads the
  list. No framework, no build step, no third-party requests.

### Android share sheet

Not in this change: the Android app shares URLs as today. A follow is created
in the web Settings panel. The change is not small (new screen, outbox-free
mutation path, tests), so the spec defers it.

## Limits

- One listing per source at a time; at most 2 listings run concurrently server
  wide (`SOURCE_CHECK_CONCURRENCY`).
- `follow_poll_seconds` floor is 300 s; the failure backoff caps at 1 h.
- The listing window is 200 posts (`PROFILE_LIST_MAX`); the backfill cap
  defaults to 50.
- Per-user follow count is not capped in this change; the cap plus the poll
  floor bound the model cost per source.

## Error cases

- Listing failure (private, unavailable, region, bot challenge, timeout):
  `last_error` shows the mapped reason (`_tiktok_failure`), `last_check` stays
  at the last success, nothing is marked seen, nothing queued, and
  `next_check` moves out by the existing backoff. A broken listing therefore
  shows as an error on the source row, never as "up to date".
- Capture failure inside the check loop (`accept_capture` raising: conflict
  or oversized validation, full or read-only disk, DB lock): the seen marker
  and its capture roll back together (nothing consumed), `last_error` shows
  the failure, `failures` counts it, and `next_check` moves out by the same
  backoff as a listing failure. `last_check` stays at the last success. The
  check returns "error" instead of raising, so a persistent failure lists at
  most once per backoff window — never in a tight loop — and the source row
  shows the error, never "up to date". `last_error` is never empty on a
  failure: a message-less exception stores the exception class name.
- Invalid profile URL or tag on create: 400, nothing stored.
- Duplicate follow: 409, nothing stored.
- A paused source is never listed and shows `Paused`; resume returns it to the
  normal schedule without changing `next_check`.
- The worker never lets a source error kill the coordinator: `check_source`
  catches listing and capture failures and returns.

## Acceptance checks

Tests use a fake yt-dlp (mocked `subprocess.run` / listing function):

1. First listing of a source with more posts than the cap queues exactly the
   cap (newest first) and marks every listed post seen.
2. A second listing whose window holds 2 new ids queues exactly those 2.
3. A repeated listing with no new ids queues nothing.
4. A failed listing (bot challenge stderr) queues nothing, marks nothing seen,
   stores the mapped error, keeps `last_check` unchanged, and moves
   `next_check` out by the backoff.
5. Two concurrent listings of the same source with the same new ids queue each
   post exactly once (the unique constraint decides; the loser queues 0).
6. The feed: entries filter by tag and by `since`; another user's entries never
   show; a missing or naive `since` answers 400.
7. The follow API: create, pause, resume, unfollow, duplicate 409, foreign
   follow 404.
8. An always-failing capture path: `worker.main(once=True)` returns, the
   listing runs at most twice, and the source row holds `failures == 1`, a
   non-empty `last_error`, and a future `next_check`.
9. `sh scripts/test.sh` and `ruff check` exit 0.

Visible-UI proof: screenshots of the Followed sources section at desktop width
and 375 px.

## Non-goals

- Platforms other than TikTok (the model keeps `platform` so YouTube or
  Instagram can come later).
- Any comparison against Ottr skills or tools (that is the Ottr card).
- Media downloads at listing time; the listing reads ids and URLs only.
- Editing the tag or the profile URL after creation (unfollow and re-follow
  covers it).
- Admin UI for the two settings fields (the admin API endpoint is the surface;
  defaults are sensible).
- A full backfill of posts beyond the 200-post listing window.

## Alternatives considered

- Separate `follow` queue tables and a custom scheduler process — rejected:
  the existing capture/job pipeline already owns retries, backoff, blocked
  states and the Inbox; a second pipeline would duplicate it.
- Polling from the web process (Celery/cron style) — rejected: the deployment
  has exactly two roles, and the worker coordinator already holds the exclusive
  lock and a thread pool.
- Storing the topic tag only on the FollowedSource and tagging at feed read
  time — rejected: entries must be tag-filterable in the Library too, and the
  contribution-tags seam already exists.

## Risks

- yt-dlp's TikTok profile extractor breaks often and hits bot challenges. The
  failure mapping, the kept `last_check`, and the error on the source row make
  a broken listing visible instead of silent.
- Model cost per post. The backfill cap, the poll floor, the pause switch and
  the 200-post window bound it.
- Profile polling is automated scraping of TikTok. This self-hosted build
  lists only public profiles from the operator's own server. A future hosted
  mode must revisit terms of service, rate limits and per-instance identity
  before enabling follows.
