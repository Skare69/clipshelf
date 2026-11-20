# Accessibility

## Our commitment

We want Clipshelf to work for people who use a keyboard only, a screen reader,
a dark theme, or reduced motion. We do not claim a WCAG conformance level, and
no test in this repository checks one.

## Supported environments

- The web UI in a current desktop or phone browser.
- The Android app on Android 8.0 and newer (`minSdk 26` in
  `android/app/build.gradle`).

## What the web UI does today

These facts come from `clipshelf/templates/` and `clipshelf/static/`:

- The page declares `lang="en"` and follows the `light dark` color scheme.
- It honors `prefers-reduced-motion` and turns animations off
  (`static/clipshelf/app.css`).
- Keyboard focus has a visible outline (`:focus-visible`), and `/` moves
  focus to the search input (`static/clipshelf/panels.js`).
- Icon-only buttons carry `aria-label` text.
- Notices use a polite live region (`role="status"`, `aria-live="polite"`).
- Form errors use `role="alert"`, and import progress uses
  `role="progressbar"` with value attributes.
- Decorative marks use empty `alt` text.

Video captures keep their ASR captions as entry text, so the words in a video
stay readable.

## Known limits

- The UI text is English only. There are no translations yet.
- The Android app labels few views for TalkBack. Today it sets one
  `contentDescription`. We have not audited TalkBack navigation.
- Media thumbnails carry no `alt` text. The model summary text is the
  description of a capture.
- We have not audited color contrast.

## How to report a barrier

Open a
[bug report](https://github.com/Skare69/clipshelf/issues/new?template=bug_report.yml)
and add the `accessibility` label. If you cannot add labels, put the word
"accessibility" in the title. Describe your environment and the barrier.
