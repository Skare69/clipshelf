# Product

<!-- impeccable:product-schema 1 -->

## Platform

adaptive

Clipshelf ships as a web app (any browser, desktop and phone) and as a native Android app. There is no iOS app. The web app follows its own visual system. The Android app themes the same system through Material roles.

## Users

- The operator: one person who runs the server, holds the admin role, and uses the library every day.
- A few invited people: family, friends, or a small team. Each person has a private Personal collection and can join shared collections.
- The usual scene: a person sees a link on the phone (TikTok, Instagram, GitHub, an article), shares it to Clipshelf, and comes back later on a desktop or a phone to find it, read what it holds, and copy a prompt, a command, or a link.

## Product Purpose

Share a link from your phone, and your own server fetches what is actually in the post: TikTok photo carousels slide by slide with their audio, video with its captions, pages as text. A vision model that you choose turns that material into typed entries (links, repos among them, and prompts) with a summary, a category, and tags.

Success: a person finds a saved item again in seconds, reads its summary without a click, copies the prompt or the install command in one action, and sees in plain words what the server did with each capture.

## Positioning

The server reads the source material itself (carousel slides, captions, page text) and a model that the operator controls interprets it. The library holds what the post said, not only a bookmark. All data stays on the operator's server.

## Operating Context

The loop has four steps:

1. Capture: share to the Android app (a durable outbox delivers later when offline) or paste text into the web app. Each capture gets a receipt.
2. Acquire: the worker resolves each URL and fetches the public material. Blocked or login-only posts keep an honest warning and a retry.
3. Interpret: the admin-managed model endpoint reads the material and writes the entry. Optional TypeSafe screening checks for prompt injection first.
4. Browse: the Library and the Inbox, per collection, with search, categories, tags, sources, and retained files.

Imports: a TikTok browser export (`tiktok.json`) or a legacy `library.json`.

## Capabilities and Constraints

- Web views: Library, Inbox, Collections, Settings, Admin, entry details, the add dialog, the setup wizard, and the account pages (django-allauth).
- Android screens: setup, share receiver, outbox, library, entry, asset viewer, collections, collection detail, settings.
- Terms that the UI uses: capture, Inbox, Library, entry (a link or a prompt), collection (Personal or shared), category, tag, source, retained source, job.
- Web stack: Django templates, vanilla JavaScript modules, native CSS. No framework, no build step, no CDN, and no third-party requests. Fonts and images must ship from the app itself.
- Model and source text goes into the DOM through `textContent` only. URLs must be http(s). `localStorage` holds the layout preferences only.
- Android stack: Java with AppCompat and Material Components views, minSdk 26, targetSdk 35. Runtime dependencies: `androidx.work`, AppCompat 1.7.0, Material Components 1.13.0. A license gate (`check_licenses.py`, `license_inventory.toml`) checks every Android runtime dependency and the APK.
- Both light and dark themes exist on the web today.
- Install instructions are copy-only. The app never runs them.

## Evidence on Hand

- `screenshot.jpg`: the README screenshot of the web Library, made from synthetic smoke data.
- `clipshelf/static/clipshelf/favicon.svg` and `android/app/src/main/res/drawable/ic_launcher_foreground.xml`: the shared mark, a white label on a parcel-yellow square.
- The name is "clipshelf" (lowercase in the README and the web wordmark, "Clipshelf" in Android titles).
- There are no testimonials, customers, or metrics. Do not invent them.

## Product Principles

1. Finding comes first: search, filters, and categories must be fast to reach on every screen size.
2. Reading and copying is the payoff: summaries, prompts, and install commands must be legible without extra clicks, and one action copies them.
3. Status in plain words: the Inbox says what the server did, what failed, why, and what the person can do next.
4. Private by construction: no third-party requests, and all assets ship from the server.
5. One product on two platforms: the same words, the same states, and the same visual system on the web and on Android.

## Accessibility & Inclusion

Keep the current commitments from the code: a skip link, visible focus, `prefers-reduced-motion` support, live status announcements, and labels on icon buttons. Text and controls must meet WCAG 2.2 AA contrast in both themes. Android touch targets must be at least 48 dp.
