# Clipshelf domain glossary

Consolidated working vocabulary. Implementation is approved per
`research/server-plan.md` (rev. 6+); this file names the concepts so code,
tests, and architecture reviews use the same nouns.

## Concepts

- **Capture** — one durable share accepted by the server (text and/or URL)
  from the Android client or the web app. Immutable once accepted; carries a
  `client_request_id` that makes delivery retries idempotent.
- **Collection** — the access boundary. Every user has a private **Personal**
  collection; shared collections have explicit members. Captures land in one
  destination collection.
- **Job** — one processing attempt pipeline for a capture URL: acquisition →
  interpretation → publication. Its row owns the lifecycle transitions
  (`mark_running`, `defer`, `mark_blocked`, `mark_done`, `fail`, `requeue` —
  field rules; callers own transactions) and the projections clients render
  (`active`, `needs_attention`, `can_retry`, `guardrail`,
  `screening_warnings`); browsers and the Android client render those instead
  of re-deriving state rules.
- **Acquisition** — fetching and bounding the public material behind a capture
  URL (page, TikTok, carousel, browser import). `clipshelf/acquisition.py`
  owns it; all egress goes through `clipshelf/network.py`.
- **Interpretation** — turning acquired material into **findings** (summary,
  repos, prompts, links, installs) via the one admin-managed
  OpenAI-compatible endpoint. `clipshelf/interpretation.py` owns the request,
  validation, and the capability check.
- **Guardrail** — a job blocked by TypeSafe screening because the captured
  material tried to steer the interpreter or the findings scored dangerous.
  Projected as `Job.guardrail`; the error text explains the withholding.
- **Screened** — a job that ran with screening warnings (fail-open):
  projection `Job.screening_warnings`.
- **Screening** — the optional TypeSafe System One guard rail (key managed in
  Admin → Screening, env `TYPESAFE_API_KEY` as fallback); policy thresholds in
  `clipshelf/judgment.py`.
- **Findings** — the validated interpretation result stored on the Job and
  published into the Library.
- **Publication** — turning findings/imports into **Entries** and
  **Contributions**. `clipshelf/publication.py` owns entry identity
  (redirect-resolved URL; SHA-256 for prompts), tombstones, and contribution
  writes for every producer.
- **Entry** — one Library item (link, prompt, …) identified per publication
  rules; contributions carry who contributed what, when.
- **Asset** — retained media published under a per-publication directory;
  `clipshelf/asset_files.py` owns custody and containment.

## Module seam conventions

- `judgment.py` is deliberately framework-free: Django resolves the screening
  key and passes it in; tests fake the transport at the module's documented
  seam.
- Browsers get decisions, not raw state: new client-visible semantics ship as
  Job projections first (see Job above), then chips/badges render them.
