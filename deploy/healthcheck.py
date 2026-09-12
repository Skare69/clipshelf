#!/usr/bin/env python3
"""Web-role container healthcheck: probe the app's own readiness endpoint.

GET /healthz is provided by the HTTP surface: it runs a real DB check
(SELECT 1 plus the ServerSettings row) and answers 200 {"ok":true,...} or
503. This script is a shell-free wrapper so the Docker healthcheck exercises
exactly what a client would see. WAL/synchronous enforcement on the volume is
verified separately by deploy/check_sqlite.py.
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8000/healthz"
# Production redirects plain HTTP to HTTPS; the ingress terminates TLS and
# forwards this header, so the local probe must present it the same way.
REQUEST = urllib.request.Request(URL, headers={"X-Forwarded-Proto": "https"})

try:
    with urllib.request.urlopen(REQUEST, timeout=10) as response:
        body = json.load(response)
        if response.status == 200 and body.get("ok") and body.get("database"):
            print("healthcheck: ok — /healthz reports database ready")
            sys.exit(0)
        print(f"healthcheck: unhealthy — /healthz body {body!r}", file=sys.stderr)
except Exception as exc:  # noqa: BLE001 — any failure is unhealthy
    print(f"healthcheck: unhealthy — {URL}: {exc}", file=sys.stderr)
sys.exit(1)
