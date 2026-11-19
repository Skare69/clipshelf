# Development

This file describes how to run the app locally for development, the code
layout, the tests, and the dependency pinning rules.

## Run it locally

```
python3.13 -m venv .venv && .venv/bin/python -m pip install --require-hashes -r requirements.lock
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data .venv/bin/python clipshelf.py serve 127.0.0.1:8000   # shell 1
CLIPSHELF_DEBUG=1 CLIPSHELF_DATA_DIR=./data .venv/bin/python clipshelf.py worker                 # shell 2
```

The first-start details (setup wizard, admin recovery, mail) are in [install.md](install.md#run-it-locally).

## Layout

The product is four pieces:

| Piece | What it does |
|---|---|
| Django app (`clipshelf/`) | accounts, collections, capture receipts, library/search, source viewing, admin |
| Worker (`clipshelf.py worker`) | acquisition (pages, TikTok video/carousels), interpretation, retries |
| Android app (`android/`) | share-sheet capture with a durable outbox, background delivery, online browsing |
| Container (`Dockerfile`, `compose.yaml`) | one image, web + worker roles, SQLite and retained media on one data volume |

The source paths:

| path | what |
|---|---|
| `clipshelf/models.py`, `services.py` | collections, captures, jobs, entries/contributions, assets |
| `clipshelf/views.py`, `urls.py` | JSON API and app shell |
| `clipshelf/accounts.py`, `admin_views.py`, `api.py` | invitation admission, native sessions, the shared JSON envelope, admin endpoints |
| `clipshelf/network.py`, `acquisition.py`, `interpretation.py` | bounded fetching, extractors, model calls |
| `clipshelf/imports.py` | import engine and findings merge |
| `clipshelf/worker.py`, `management/commands/` | job coordinator and operator commands |
| `clipshelf/templates/`, `static/` | web UI |
| `android/` | native client |
| `research/` | the plan and the research behind it |

## Tests

Tests: `sh scripts/test.sh` is the one test list, and CI runs the same file.
It runs after the install, license, lint and audit gates, with `PY` set to an
interpreter that has `requirements.lock` installed (the local setup above uses
`.venv/bin/python`). Run it with a throwaway data dir, never `./data` (the
app writes its secret key there):
`CLIPSHELF_DATA_DIR=/tmp/clipshelf-test PY=.venv/bin/python sh scripts/test.sh`.
It covers the offline Python suites, including the seeded fuzz of the
trust-boundary parsers in `test_properties.py` (150 runs per property, no new
dependencies), the JS regression checks, and the mutation harness self-check
(`mutate.py self-check`); CI uses Node 24. The android job builds the debug
APK, runs `check_licenses.py android` on it, then
`./gradlew :app:testDebugUnitTest` and `./gradlew :app:lintDebug`. A lint
warning fails the build.

## Mutation testing

`python mutate.py` checks that the tests actually pin the domain logic: it
generates single-site mutants (operator swaps, constant tweaks) of the target
modules and runs the normal offline test suite once per mutant. A mutant the
suite still passes is a survived mutant — a behavior no test guards.

- Stdlib only (Python 3.13, `ast` + `unittest`), no new dependencies; the
  mutation tool never ships in the runtime image.
- Safety: mutants run in a workspace under `TEMP/clipshelf-mutation/` that
  holds only the git-tracked files (gitignored personal data, caches and
  keystores are never copied), with a per-mutant throwaway
  `CLIPSHELF_DATA_DIR`; the checkout is never modified, mutant runs execute
  only the offline test suite (no network, no other commands), and nothing
  the harness creates is deleted.
- Scope: `DEFAULT_TARGETS` in `mutate.py` — the pure-logic domain modules.
  For a change-sized run use `python mutate.py --diff origin/main`; view,
  command and gate-script modules (`scripts/`, `deploy/`,
  `check_licenses.py`) join the scope the same way. Tests, migrations and
  wiring (settings, urls) are never mutated (the suite is the oracle), and
  modules a branch deletes are skipped instead of failing the run.
- Release review: `python mutate.py --diff <previous tag> --max-mutants 116`
  samples 116 evenly spaced sites from every in-scope module the release
  touched, gate scripts included.
- Gate: use the project Python 3.13 virtual environment and run
  `python mutate.py --max-mutants 116` from the repository root. For each of
  exactly 116 evenly spaced sites across `DEFAULT_TARGETS` (listed file/AST
  order), it runs only the tests that a baseline coverage pass caught on the
  mutated line; import-time lines run the whole suite commands that import
  the target. A line no test reaches reports NO_COVERAGE and counts like a
  survived mutant. For site count `S`, the chosen
  indices are `floor(i*S/116)` for `i = 0..115`; use the same commit and target
  list to repeat a score. The `DEFAULT_FAIL_UNDER` floor is 94%; never lower it.
- The old stride sampler did **not** select the requested count: 950 sites with
  `--max-mutants 116` gave stride 9 and just 106 mutants (73/106 = 68.9% in
  the release review). The earlier 94/116 = 81.0% record cannot come from
  the 950-site tree with that sampler; an older 921-site tree would select 116
  at stride 8. The scores cannot be compared as a ratchet. Membership drift
  moves the evenly-spaced sampler the same way: growing the scope from 946
  to 956 sites swapped 99 of the 116 sampled sites.
- A suite that exits non-zero always kills the mutant, so environmental noise
  (file locks, timeouts, flaky tests) can only raise a score, never lower it.
  The clean baseline of the fixed harness is 92/116 = 79.3% on the 971-site
  scope: 0 timeouts, no environmental kills. Earlier readings must not be
  the baseline: the review's 76.7% was inflated by a stale-bytecode defect
  (a mutant could inherit the previous same-size mutant's `__pycache__`
  entry and fail on bytecode it never generated — false kills), and it ran
  on a 946-site sample sharing only 17/116 sites with this one; the 956-site
  tree measured 66.4% before the transport, admin-endpoint and CSRF tests
  landed. The 24 baseline survivors are documented coverage gaps (judgment
  screening strings, interpretation message and bound constants,
  `MAX_URL_LENGTH`, asset and lib error strings); pin them with tests, then
  raise the floor. Inspect `KILLED` diagnostics before raising it. A timeout
  counts as a kill, not evidence that a test caught the mutant.
- `python mutate.py self-check` verifies the harness. A sample is a practical
  gate, not the full-scope score; use `python mutate.py` for all sites.

## Dependency and build pinning

`requirements.txt` holds the version ranges. CI, the published image and the
local setup in this file install one hash-locked resolution of it — `requirements.lock`,
with per-platform wheel hashes for Windows development and linux/amd64 + arm64 —
so the audited set is the shipped set:

- `python scripts/lock_deps.py check` fails when `requirements.txt` would
  resolve differently from the lock (CI runs this gate): a changed pin, a
  missing pin, an extra pin in the lock that is unrelated to the current
  resolution, or a yanked pin (pip installs yanked files under exact pins and
  pip-audit ignores yank status, so they must never ship). When the drift is
  changed pins, check also prints a ready-to-use conventional commit subject
  computed from those pins, with bump classes major, minor, patch, or change.
  The one known
  exception is Django's win32-only `tzdata` marker dependency, which may be
  present in the lock but absent from a non-Windows resolution. It also fails
  when any shipped platform (Windows, linux/amd64, linux/arm64) lacks a wheel
  matching the lock's hashes.
- `python scripts/lock_deps.py make` regenerates the lock — after changing
  `requirements.txt`, or when the check fails on a new upstream release. It
  writes nothing when any platform has no wheel for a pin, and fails on
  yanked pins (pin an older release in `requirements.txt` instead) or on
  ungated additions that must be listed in `--fresh`.
- Both commands resolve only releases uploaded at least 7 days ago, so a new
  upstream release enters the lock (and turns the check red) one week after it
  appears; that age-gated pass resolves a floor-free copy of
  `requirements.txt` (lower bounds dropped, upper bounds kept), so a floor
  raised to a release younger than 7 days does not break the gate while the
  copy still resolves inside the real range. A newer pin already in the lock
  stays, and every keep
  pin (including marker-gated ones like `tzdata`) is held in the always-run
  held pass against the real `requirements.txt`.
  `python scripts/lock_deps.py make --fresh django` admits a security release
  of `django` at once; it exits naming the held pins to add when the fresh
  name does not move. On non-Windows hosts the lock's `tzdata` pin is carried
  into the resolution verbatim.
- `pip-audit` in CI audits the lock directly (`--no-deps`), never a fresh
  re-resolution.

The image base is pinned by manifest digest (`python:3.13-slim@sha256:...`)
and every GitHub Action is pinned to a commit SHA. Bump these deliberately,
one review per bump; the publish workflow additionally verifies the built
image's installed packages against the lock before pushing.

## Licenses

Bundled third-party code is inventoried in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) and gated by
`check_licenses.py` against `license_inventory.toml`. The bundled Archivo font
is OFL-1.1, which the inventory allows for font files only.
`check_licenses.py` does not scan fonts, so its notice and `[fonts.entries]`
change by hand.
