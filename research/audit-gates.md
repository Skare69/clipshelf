# Applicable architecture audit gates (Python and Gradle)

Decision record for the architecture review of 2026-10-02: which
dependency-audit gates apply to this repository, which do not, and the
observed license inventory behind the answer. Snapshots in this file are
dated evidence, not gates — enforcement lives in CI, not in this document.

## Stack inventory (what has an audit surface)

| Stack | Manifest | Direct deps | Audit surface |
|---|---|---|---|
| Python server | `requirements.txt` ranges, installed from hash-pinned `requirements.lock` | 8 | `pip-audit -r requirements.lock --no-deps`, `ruff check`, `scripts/lock_deps.py check` |
| Android client | `android/app/build.gradle` (exact pins) | 1 | `gradlew :app:dependencies --configuration releaseRuntimeClasspath` |
| JavaScript | none | 0 | none — see below |

No `package.json`, `bun.lock`, `package-lock.json`, `yarn.lock`, or
`pnpm-lock.yaml` is tracked (`git ls-files`, checked 2026-10-02). The only
shipped JavaScript is `tiktok-extract.js`, a paste-into-DevTools console
script with no imports and no build step. `bun audit --audit-level=high` and
`bun pm licenses` exit 1 here with `No package.json was found` — the gates
have no subject. Do not add an empty manifest to make them run; that would
audit nothing. If a real JavaScript build lands, these gates become
applicable again and this record must be revised.

## Gate policy

| Gate | Status | Where |
|---|---|---|
| Python vulnerability audit | Applicable, enforced | `ci.yml` python job: `pip-audit -r requirements.lock --no-deps` (green 2026-10-03: "No known vulnerabilities found") |
| Python lint | Applicable, enforced | `ci.yml`: `ruff check`, scoped by `ruff.toml` to the `clipshelf` package |
| Dependency lock consistency | Applicable, enforced | `ci.yml`: `python scripts/lock_deps.py check` (31 pins match the current resolution) |
| Python mutation testing | Applicable, tooling on main | `mutate.py` (from `ott/27f2220b`) runs locally; it is not a CI gate |
| Dependency license inventory + allowlist | Applicable, enforced | `ci.yml` runs `check_licenses.py` against `license_inventory.toml` in both jobs (green 2026-10-03: python 31 pins, android 30 artifacts); the tables below are the observed snapshot behind it |
| Bun/JS vulnerability + license audit | Not applicable | No manifest exists (see stack inventory) |
| Gradle vulnerability audit | No first-party scanner; licenses enforced | `ci.yml` android job resolves `releaseRuntimeClasspath` and gates it through `check_licenses.py android`; exact pins in `build.gradle`, wrapper pins Gradle 8.9 and AGP 8.7.3; Gradle-artifact vulnerability scanning remains a gap |
| Docker image / OS-package audit | Pins enforced; no OS-package vulnerability scan | `ci.yml` python job runs `python check_licenses.py`, whose default run includes the image check: the Dockerfile base image, apt stage sets, and SQLite ARGs must match `license_inventory.toml`, and the Dockerfile must COPY `THIRD_PARTY_NOTICES.md`. It reads the Dockerfile, not a built image. `docker-publish.yml` (v* tag or manual dispatch only) builds the amd64 image and fails when its installed Python set differs from `requirements.lock`. Debian-package vulnerability scanning remains a gap |

## Observed license inventory (snapshot 2026-10-02)

Python rows come from `importlib.metadata` in the CPython 3.13 venv on the
snapshot date. Since 8cfe6d9, CI and the image install
`pip install --require-hashes -r requirements.lock`, so the lock, not this
record, pins the shipped versions. The
Android rows come from the resolved `releaseRuntimeClasspath` tree and the
`<licenses>` block of each artifact's POM in the local Gradle cache. The
enforced, machine-checked inventory is `license_inventory.toml`, gated by
`check_licenses.py` in CI; update that file, not this record, when
dependencies change.

### Python — direct (8)

| Distribution | Observed | Declared license |
|---|---|---|
| Django | 5.2.17 | BSD-3-Clause |
| django-allauth | 65.19.3 | MIT |
| waitress | 3.0.2 | ZPL-2.1 |
| Pillow | 12.3.0 | MIT-CMU |
| gallery-dl | 1.32.12 | GPL-2.0-only |
| yt-dlp | 2026.8.19 | Unlicense |
| argon2-cffi | 25.1.0 | MIT |
| typesafe-sdk | 0.7.0 | MIT |

`gallery-dl` is the only copyleft entry: GPL-2.0-only is acceptable because
it runs as a separate executable subprocess (as does the Debian `ffmpeg`
package — an OS dependency, not a pip one), never as imported or linked code
inside the MIT application.

### Python — transitive (18, same resolution)

| Distribution | Observed | Declared license |
|---|---|---|
| asgiref | 3.12.1 | BSD-3-Clause |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| curl-cffi | 0.16.3 | MIT |
| httpcore2 | 2.13.0 | BSD-3-Clause |
| httpx2 | 2.13.0 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| sqlparse | 0.6.0 | not declared in wheel metadata; tracked as BSD in `license_inventory.toml` |
| tenacity | 9.1.4 | Apache-2.0 |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| argon2-cffi-bindings | 26.1.0 | MIT |

`yt-dlp[curl-cffi]` is the only extra-selecting line; `mutagen`, `websockets`,
and `yt-dlp-ejs` remain optional yt-dlp extras and are not part of this
resolution.

### Android — runtime (`releaseRuntimeClasspath`, resolved 2026-10-02)

Direct: `androidx.work:work-runtime:2.9.1`. Every POM in the resolved tree
declares "The Apache Software License, Version 2.0" except
`com.google.guava:listenablefuture:1.0`, whose POM carries no
`<licenses>` block at all (its upstream source is Apache-2.0); that gap is
tracked in the enforced inventory. Build-time and test-scoped tooling — Gradle wrapper 8.9,
AGP 8.7.3, JDK 17, junit (`testImplementation`) — is not shipped and stays out
of this inventory.

| Group | Artifacts |
|---|---|
| `androidx.work` | work-runtime 2.9.1 |
| `androidx.annotation` | annotation 1.3.0, annotation-experimental 1.3.0 |
| `androidx.arch.core` | core-common 2.1.0, core-runtime 2.1.0 |
| `androidx.collection` | collection 1.0.0 |
| `androidx.concurrent` | concurrent-futures 1.0.0 |
| `androidx.core` | core 1.9.0 |
| `androidx.lifecycle` | lifecycle-common 2.5.1, lifecycle-livedata 2.5.1, lifecycle-livedata-core 2.5.1, lifecycle-runtime 2.5.1, lifecycle-service 2.5.1 |
| `androidx.room` | room-common 2.5.0, room-ktx 2.5.0, room-runtime 2.5.0 |
| `androidx.sqlite` | sqlite 2.3.0, sqlite-framework 2.3.0 |
| `androidx.startup` | startup-runtime 1.1.1 |
| `androidx.tracing` | tracing 1.0.0 |
| `androidx.versionedparcelable` | versionedparcelable 1.1.1 |
| `com.google.guava` | listenablefuture 1.0 (no license in POM — see above) |
| `org.jetbrains` | annotations 23.0.0 |
| `org.jetbrains.kotlin` | kotlin-stdlib 1.8.22, kotlin-stdlib-common 1.8.22, kotlin-stdlib-jdk7 1.8.20, kotlin-stdlib-jdk8 1.8.20 |
| `org.jetbrains.kotlinx` | kotlinx-coroutines-android 1.7.1, kotlinx-coroutines-core 1.7.1, kotlinx-coroutines-core-jvm 1.7.1 |

(`kotlinx-coroutines-bom` 1.7.1 appears only as a platform constraint, not a
shipped artifact.)

## Reproducing the snapshot

- `python scripts/lock_deps.py check` — lock consistency gate.
- `pip-audit -r requirements.lock --no-deps` — vulnerability gate, must stay
  green in CI.
- `pip install --require-hashes -r requirements.lock` into a clean CPython 3.13
  venv, then `python check_licenses.py` — the enforced python license gate (a
  dev venv with extra tooling installed trips it by design; CI installs only
  the lock).
- `cd android && ./gradlew :app:dependencies --configuration
  releaseRuntimeClasspath --no-daemon -q > app/build/license-deps.txt && python
  ../check_licenses.py android --deps android/app/build/license-deps.txt` —
  the enforced android license gate, as CI runs it.
- `git ls-files | grep -iE 'package\.json|bun\.lock|.*lock'` — must return
  nothing for the JS gates to stay "not applicable".
