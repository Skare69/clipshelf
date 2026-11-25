#!/bin/sh
# The one test list: CI (.github/workflows/ci.yml) and the agents' "Verified" block
# (AGENTS.md) both run this file. Add a new test here, in the same commit as the test.
# Run it after the install, license, lint and audit gates. PY is the interpreter that
# has requirements.lock installed (CI: python, agents: their throwaway venv).
set -eux
cd "$(dirname "$0")/.."
: "${CLIPSHELF_DATA_DIR:?set CLIPSHELF_DATA_DIR to a throwaway dir, never ./data}"
PY=${PY:-python3}
# Page renders start the GitHub release check; tests never reach the network.
export CLIPSHELF_UPDATE_CHECK=0
# CI (ci.yml env) and the agents' Verified block both set CLIPSHELF_DEBUG=1.
# Export it here too, so the documented local command also works on hosts
# whose SQLite is below the verified-WAL floor (settings.py raises
# ImproperlyConfigured there when DEBUG is off).
export CLIPSHELF_DEBUG=1
$PY clipshelf.py check
# --noinput: a leftover test-db.sqlite3 must never hang the run on Django's
# "delete the test database?" prompt (CLIP-104 merge-turn incident).
$PY clipshelf.py migrate --noinput
$PY clipshelf.py test --noinput --verbosity 1
$PY test_clipshelf.py
$PY test_sources.py
$PY test_follows.py
# Lock gate: check is clock-independent (CLIP-102 age-gate replay on a fake index).
$PY test_lock_deps.py
# Property checks: the trust-boundary parsers under a seeded stdlib fuzz loop.
$PY test_properties.py
# Publish-tag regression: docker-publish derives tags from the validated
# version, never the raw GITHUB_REF_NAME (v0.11.0 release-review finding).
$PY test_workflow_tags.py
# JS regression checks pinning the v0.10.0 fixes, plus the mutation
# harness's own target-confinement/sampling asserts.
node test_dom_events.mjs
node test_web_assets.mjs
node tiktok-extract.js
node whatsapp-extract.js
$PY mutate.py self-check
# Inbox tracking line: each job state maps to its route and status text.
node test_inbox_status.mjs
# Release-workflow gate pins: signed-apk (and attach-apk) need wait-ci.
node test_release_gates.mjs
# Category rail: the counts cover the whole library, not only the loaded page.
node test_cat_rail.mjs
# Inbox keeps polling after "Try again" restarts a blocked job.
node test_inbox_retry_poll.mjs
# Follow form: "Suggest tags" chips fill the Topic tag; inline errors, no banner.
node test_follow_tags.mjs
