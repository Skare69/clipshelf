#!/bin/sh
# Host-runnable outbox invariant check (plain JDK, no Android SDK needed).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(mktemp -d)"
javac -encoding UTF-8 -d "$OUT" "$HERE/OutboxPolicyCheck.java" \
  "$HERE/../app/src/main/java/dev/clipshelf/app/outbox/OutboxPolicy.java"
java -cp "$OUT" OutboxPolicyCheck
