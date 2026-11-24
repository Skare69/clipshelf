"""Newer-release check for the footer notice (CLIP-127).

available_update() never blocks a page render: it answers from the in-memory
cache and, when the cache is stale, starts one background check. A good answer
is trusted for an hour. A failed check keeps the last good answer (none on a
cold start: never a guessed update) and waits 60 s before github is asked
again. CLIPSHELF_UPDATE_CHECK=0 skips the check, so no request leaves the server.
"""
import json
import logging
import os
import re
import threading
import time

from clipshelf import network

log = logging.getLogger(__name__)

LATEST_URL = "https://api.github.com/repos/Skare69/clipshelf/releases/latest"
RELEASE_PREFIX = "https://github.com/Skare69/clipshelf/releases/"
FRESH_S = 3600
FAILURE_BACKOFF_S = 60
_NUMERIC = re.compile(r"\d+(\.\d+)*")

_lock = threading.Lock()
_state = {"at": None, "update": None, "fail_until": 0.0, "running": False}


def is_newer(latest, current):
    """True when latest is strictly newer; unparsable input (dev, prerelease
    suffix, garbage) compares False, so the notice never claims an update it
    cannot prove."""
    if not (_NUMERIC.fullmatch(latest) and _NUMERIC.fullmatch(current)):
        return False
    a = [int(x) for x in latest.split(".")]
    b = [int(x) for x in current.split(".")]
    width = max(len(a), len(b))
    return a + [0] * (width - len(a)) > b + [0] * (width - len(b))


def available_update(current):
    """{"version", "url"} of a newer stable release, else None."""
    if os.environ.get("CLIPSHELF_UPDATE_CHECK") == "0":
        return None
    if not _NUMERIC.fullmatch(current):
        # is_newer() refuses a non-numeric current (dev, prerelease suffix),
        # so a check for one is a guaranteed-useless request: never start it.
        return None
    now = time.monotonic()
    with _lock:
        stale = _state["at"] is None or now - _state["at"] >= FRESH_S
        start = stale and now >= _state["fail_until"] and not _state["running"]
        if start:
            _state["running"] = True
    if start:
        threading.Thread(target=_check, args=(current,), daemon=True).start()
    with _lock:
        return _state["update"]


def _check(current):
    try:
        update = _fetch(current)
    except (network.NetworkError, ValueError) as exc:
        log.warning("release check failed (%s); retrying in %ss", exc, FAILURE_BACKOFF_S)
        with _lock:
            _state["fail_until"] = time.monotonic() + FAILURE_BACKOFF_S
    else:
        with _lock:
            _state.update(at=time.monotonic(), update=update, fail_until=0.0)
    finally:
        # an unexpected error still frees the next check (and its traceback
        # reaches the thread's excepthook)
        with _lock:
            _state["running"] = False


def _fetch(current):
    body = json.loads(network.fetch_public(LATEST_URL, max_bytes=1 << 20, timeout=10)["body"])
    if not isinstance(body, dict):
        raise ValueError("unexpected github payload")
    tag, url = body.get("tag_name"), body.get("html_url")
    # html_url becomes the notice href: only this repo's release pages pass
    if not (isinstance(tag, str) and isinstance(url, str) and url.startswith(RELEASE_PREFIX)):
        raise ValueError("unexpected github payload")
    latest = tag.removeprefix("v")
    return {"version": latest, "url": url} if is_newer(latest, current) else None


def reset():
    """Test seam: forget the cached answer and the failure window."""
    with _lock:
        _state.update(at=None, update=None, fail_until=0.0, running=False)
