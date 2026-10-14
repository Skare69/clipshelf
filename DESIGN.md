---
name: Clipshelf
description: A saved-link shelf drawn as white shipping labels on a warehouse-gray ground, with one parcel-yellow signal.
colors:
  ground: "light-dark(#E9EBEE, #0E1013)"
  label: "light-dark(#FFFFFF, #1A1D22)"
  label2: "light-dark(#F3F4F6, #22262C)"
  ink: "light-dark(#15171A, #EEF0F3)"
  ink2: "light-dark(#4A5059, #B2B9C3)"
  ink3: "light-dark(#646B75, #8D95A0)"
  hair: "light-dark(#D4D8DE, #2E333B)"
  edge: "light-dark(#BCC2CA, #454C56)"
  field: "light-dark(#767E88, #7D8590)"
  signal: "#FFC629"
  on-signal: "#15171A"
  signal-edge: "light-dark(#15171A, #FFC629)"
  ok: "light-dark(#18794E, #4CC38A)"
  ok-soft: "light-dark(#E5F3EC, #12291E)"
  err: "light-dark(#C0172B, #FF7A7A)"
  err-soft: "light-dark(#FCE9EA, #351A1C)"
typography:
  t1:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 700
  t2:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
  t3:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  t4:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 800
  t5:
    fontFamily: "Archivo, system-ui, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "-0.015em"
  mono:
    fontFamily: "ui-monospace, Cascadia Mono, Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
rounded:
  label: "6px"
  ctl: "4px"
  dlg: "8px"
  block: "2px"
spacing:
  xs: "8px"
  sm: "12px"
  md: "16px"
  lg: "24px"
  xl: "28px"
  xxl: "36px"
components:
  button-signal:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.on-signal}"
    rounded: "{rounded.ctl}"
    height: "40px"
    padding: "0 16px"
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.label}"
    rounded: "{rounded.ctl}"
    height: "40px"
    padding: "0 16px"
  button-outlined:
    backgroundColor: "{colors.label}"
    textColor: "{colors.ink}"
    rounded: "{rounded.ctl}"
    height: "40px"
    padding: "0 16px"
  routing-block:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.label}"
    rounded: "{rounded.block}"
    padding: "6px 10px 5px"
  label-card:
    backgroundColor: "{colors.label}"
    textColor: "{colors.ink}"
    rounded: "{rounded.label}"
    padding: "14px 16px 12px"
  chip:
    backgroundColor: "{colors.label2}"
    textColor: "{colors.ink2}"
    rounded: "{rounded.ctl}"
    height: "24px"
    padding: "0 8px"
  input-search:
    backgroundColor: "{colors.label2}"
    textColor: "{colors.ink}"
    rounded: "{rounded.ctl}"
    height: "40px"
---

# Design System: Clipshelf

## Overview

**Creative North Star: "Parcel route"**

Clipshelf looks like a shipping dock for saved links. Every saved item is a white die-cut label on a cool warehouse-gray ground. A heavy capital routing block states the category. Small bold field names read like print on a courier label: From, Contents, date. A dashed perforation sits above each label's actions. One capture is one tracked shipment, and the Inbox shows its route with a four-step tracking line: Received, Fetched, Read, Shelved.

The system is print-like, not app-like. Edges come from 1 px borders, not from shadows. The palette is almost monochrome. Parcel yellow (#FFC629) appears once per screen, on the next action. Red appears only on exceptions. There is no decoration, no thumbnail, and no pill. Dark mode is the same warehouse at night: graphite labels on near-black.

**Key Characteristics:**
- White labels (6 px corners, 1 px edge border) on a cool gray ground.
- Black routing blocks in heavy, condensed, uppercase Archivo.
- One parcel-yellow signal per screen, on the next action only.
- Dashed perforation lines above label actions and inside shipping rows.
- Borders instead of shadows, at rest and on hover.
- A fixed five-size type ramp. Monospace only for codes and prompts.

## Colors

The palette is a grayscale with two functional colors: one signal yellow and one exception red. Tokens are CSS custom properties on `:root` that use `light-dark()`. Android mirrors them in `values/colors.xml` and `values-night/colors.xml`.

### Primary
- **Parcel Yellow** (#FFC629, same in both themes): the signal. It fills the one next action per screen (Add, primary submit), the selection highlight, and the app mark. In dark mode its border becomes yellow itself (`signal-edge`).
- **Ink** (#15171A light / #EEF0F3 dark): thermal-print text, primary button fill, routing-block fill, active nav fill.

### Neutral
- **Ground** (#E9EBEE light / #0E1013 dark): the warehouse floor behind everything.
- **Label** (#FFFFFF light / #1A1D22 dark): the label paper. Every card, button, and bar sits on it.
- **Label2** (#F3F4F6 light / #22262C dark): second-surface paper. Search fields, tags, code blocks.
- **Ink2** (#4A5059 light / #B2B9C3 dark): secondary text: sources, notes, field values.
- **Ink3** (#646B75 light / #8D95A0 dark): tertiary text: dates, counts, placeholders.
- **Edge** (#BCC2CA light / #454C56 dark): the label border, the strongest hairline.
- **Hair** (#D4D8DE light / #2E333B dark): internal dividers and perforation dashes.
- **Field** (#767E88 light / #7D8590 dark): control borders at rest.

### Functional
- **Exception Red** (#C0172B light / #FF7A7A dark): errors only. Paired with soft fills (`err-soft`).
- **Ok Green** (#18794E light / #4CC38A dark): success states only, with `ok-soft` fills.

### Named Rules
**The One Signal Rule.** Parcel yellow marks only the next action. If two yellow elements sit on one screen, the design is wrong.
**The Exception Rule.** Red appears only on errors and held shipments that need attention. Red never decorates.

## Typography

**Display Font:** Archivo, OFL-1.1. The web self-hosts one variable woff2. Android ships five static instances: regular, semibold, bold, routing, display. Fallback: the system-ui stack.
**Mono Font:** ui-monospace stack, for codes, prompts, and install commands only.

**Character:** One grotesk carries everything, like one print head. Weight and width carry hierarchy, not new fonts. Routing blocks squeeze the width to 70% and use capitals. The wordmark and the titles use 88% width and tight tracking.

### Hierarchy (web rem / Android sp)
- **T1 — Field name** (700, 13px/13sp): small bold labels, dates, counts, step names. Numbers use tabular figures (`tnum`).
- **T2 — Field value** (500–600, 14px/14sp): buttons, sources, tag text, body in tight rows.
- **T3 — Body** (400, 16px/16sp, 1.5 line height): entry titles (650), contents, default text.
- **T4 — Routing block** (800, 20px/20sp, width 70%, uppercase, +0.02em): category heads. Dialog titles reuse the size at 88% width and no caps.
- **T5 — Page title** (800, 28px, 1.1, -0.015em): one per view, 88% width.
- **Code** (mono, 14px): prompt and command blocks on `label2`.

### Named Rules
**The Five Sizes Rule.** The ramp has exactly five steps (13, 14, 16, 20, 28). No screen adds a sixth.

## Layout

The web shell is a grid: a 56 px sticky top bar (mark, search, signal Add button), a 232 px left rail (views and category index with counts), and a main column capped at 1480 px with 28/32 px padding. Labels flow in an auto-fill grid, minimum 320 px per card, 16 px gaps.

- Breakpoints: at 960 px and below, the rail becomes the bottom navigation bar, Add becomes a floating button, the category chip row appears, and entry details open as a full-screen sheet. Above 960 px, entry details open as a side panel at the right edge. At 520 px and below, the wordmark text and the "/" hint hide, and the library toolbar folds behind one Filters button.
- Spacing rhythm: 8/12/16 inside cards, 24–36 between sections, 16 px page gutter on phones.
- Dates and counts are right-aligned tabular numerals.
- Android translates the shell to Material: top app bar (56 dp), full-width search, chip row, single-column label list, bottom navigation (Library, Outbox, Collections, Settings).

## Elevation & Depth

Depth comes from borders, not shadows. Labels, rows, and fields are flat. A 1 px `edge` border separates paper from ground, and internal `hair` dividers separate fields. Hover changes the border color, never the elevation.

Only two floating elements carry a shadow: the snackbar (`0 10px 28px -8px rgb(0 0 0 / .45)`) and the phone Add button (`0 8px 20px -6px rgb(0 0 0 / .4)`). Dialogs and the side panel use a border and the backdrop, not a shadow. Android keeps cards flat (`Widget.Parcel.Card`: fill and a 1 dp stroke) and uses Material elevation only where the framework requires it.

### Named Rules
**The Borders Rule.** Flat surfaces carry a border. A shadow never replaces a border, and hover lifts nothing.

## Shapes

Small, print-like corners. Labels use 6 px, controls and chips use 4 px, dialogs use 8 px, and routing blocks are nearly square at 2 px. Interactive shapes are never pills or circles. Only the spinner, the count badge, and the tracking dots are round. Exception: the phone navigation indicator is a pill on both platforms. The web rail styles it 56×32 px with a 16 px radius. Android `Widget.Parcel.BottomNav` sets no `itemActiveIndicatorStyle`, so Material Components draws its default rounded indicator. The perforation line is a 1 px dashed `edge` border. It appears above card actions and between the links of one capture.

## Components

### Buttons
- **Shape:** 4 px corners, 40 px minimum height, 0 16 px padding, 600 weight, 14 px text.
- **Signal (`btn.signal`):** parcel-yellow fill, ink text, 700 weight, border `signal-edge` (ink in light, yellow in dark). One per screen. Hover darkens the fill 14%.
- **Primary (`btn.primary`):** ink fill, label text. Hover mixes 14% label into the ink.
- **Outlined (`btn` default):** label fill, `field` border. Hover: ink border, 6% ink wash.
- **Danger:** red fill, label text. Use it only to confirm a destructive action.
- **Quiet:** transparent. Hover shows the 6% ink wash.
- **Android:** the primary button is a Material TonalButton in signal yellow, with an ink stroke in light mode. Outlined and Text buttons carry the rest. Corners come from `ShapeAppearance.Parcel.Control` (4 dp), not pills.

### Routing block
The signature element: category head as a thermal-print box. Ink fill, label text, 800 weight, 70% width, uppercase, 20 px, 2 px corners, `6px 10px 5px` padding. A small variant (13 px) sits inside cards and detail rows. Routing blocks appear only for categories, never for tags or titles.

### Label card
A saved item as a courier label: `label` fill, 1 px `edge` border, 6 px corners, `14px 16px 12px` padding. Inside: a T1 field head (kind icon, date, right-aligned tabular), a T3 title clamped to 2 lines, From, Contents clamped to 3 lines, tags, then a dashed perforation and the action row. Hover moves the border to `field`. Android mirrors this in `item_entry.xml` with `Widget.Parcel.Card` and a drawable perforation.

### Chip / tag
Tags and category chips are 4 px rectangles, not pills. Tag: `label2` fill, `hair` border, 24 px height, T1 text, `ink2`. Category filter chip (web `.catrow`, Android `Widget.Parcel.Chip`): selected state inverts to ink fill with label text.

### Input / search
The top-bar search: 40 px tall, `label2` fill, `field` border, 4 px corners, placeholder in `ink3`. Focus-within turns the border to ink, sets the fill to `label`, and adds a 1 px ink ring. Android uses `Widget.Parcel.Field` with the same colors and 4 dp corners.

### Navigation
Web: the rail items are 40 px rows with 4 px corners. The active view inverts to ink fill with label text. An active category link gets an inset 1 px ring. Counts sit at the right in T1 tabular figures, and an alert count takes a red badge. Phone: a bottom navigation bar. Android: `Widget.Parcel.BottomNav` on `label` with ink active states.

### Tracking line (Inbox)
A four-column track: Received, Fetched, Read, Shelved. Each step is a dot on a 2 px line, with a T1 name under it. Reached steps fill with ink. When the worker reaches a new step, the line to it fills with a 0.5 s ease-out scaleX animation. A held or failed step shows a red ring, and the status line under the track gives the reason on `err-soft` with the next action. Android shows only the two phone steps (On phone, Delivered) and never shows server steps. All animation stops under `prefers-reduced-motion: reduce`.

## Do's and Don'ts

### Do:
- **Do** fill exactly one action per screen with parcel yellow (#FFC629). Everything else stays ink or outlined.
- **Do** draw depth with 1 px borders (`edge` outside, `hair` inside). Keep shadows for the snackbar and the phone Add button.
- **Do** set category heads as routing blocks: 800 weight, 70% width, uppercase, 2 px corners.
- **Do** use tabular numerals for dates and counts, right-aligned.
- **Do** keep the ramp at five sizes (13, 14, 16, 20, 28) and keep Archivo the only text face.
- **Do** put a dashed perforation above a label's action row.

### Don't:
- **Don't** use yellow for decoration, charts, warnings, or a second accent. One signal per screen.
- **Don't** use red outside exceptions and destructive confirmations.
- **Don't** use shadows for cards, rows, dialogs, or hover states. Use borders.
- **Don't** use pills, large radii, or gradients. Corners are 2 to 8 px.
- **Don't** use routing blocks for anything except categories.
- **Don't** use mono for prose. Mono is for codes, prompts, and commands only.
- **Don't** invent a sixth type size or a second font family.
