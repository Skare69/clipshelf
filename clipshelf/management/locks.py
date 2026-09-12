"""Portable process locks coordinating data access across web/worker/operator.

data_lock(exclusive=False): shared holders (serve, worker, write commands) may
run concurrently; exclusive holders (backup/restore) drain every shared holder
before proceeding — the controlled write pause for consistent snapshots.
coordinator_lock(): exactly one worker coordinator per data directory.

Implementation: byte-range locks on one lock file — POSIX fcntl.lockf and
Windows msvcrt.locking share the same scheme, so behavior matches on both.
Shared holders each lock one slot byte; an exclusive holder locks the whole
slot region. Both OSes release the locks automatically when the process dies,
so a killed backup/worker never wedges the next one. ponytail: 64 shared
holders max — plenty for one web + one worker + a short command; raise SLOTS
if a real deployment ever hits it.
"""
import contextlib
import os
import time
from pathlib import Path

from django.conf import settings

SLOTS = 64
POLL = 0.1

_held = {}  # lock file path -> [exclusive, depth] (reentrancy per process)


def _lock_path(name):
    data_dir = Path(settings.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / name)


def _open(path):
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def _try_lock(fd, exclusive, start, length):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, start, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, length)
            return True
        except OSError:
            return False
    import fcntl

    op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    try:
        fcntl.lockf(fd, op, length, start, os.SEEK_SET)
        return True
    except OSError:
        return False


def _unlock(fd, start, length):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, start, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, length)
        return
    import fcntl

    fcntl.lockf(fd, fcntl.LOCK_UN, length, start, os.SEEK_SET)


def _acquire_region(fd, exclusive, timeout):
    if exclusive:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd, True, 0, SLOTS):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "could not take exclusive data lock within "
                    f"{timeout:g}s; stop web/worker processes first"
                )
            time.sleep(POLL)
        return 0, SLOTS
    deadline = time.monotonic() + timeout
    while True:
        for slot in range(SLOTS):
            if _try_lock(fd, False, slot, 1):
                return slot, 1
        if time.monotonic() > deadline:
            raise TimeoutError("could not take shared data lock; all slots busy")
        time.sleep(POLL)


@contextlib.contextmanager
def data_lock(*, exclusive=False, timeout=30.0):
    """Hold shared (default) or exclusive access to the live data directory."""
    path = _lock_path("data.lock")
    state = _held.get(path)
    if state:
        if state[0] != exclusive:
            raise RuntimeError(
                f"data_lock({exclusive=}) nested inside "
                f"data_lock(exclusive={state[0]})"
            )
        state[1] += 1
        try:
            yield
        finally:
            state[1] -= 1
            if not state[1]:
                del _held[path]
        return
    fd = _open(path)
    try:
        region = _acquire_region(fd, exclusive, timeout)
        _held[path] = [exclusive, 1]
        try:
            yield
        finally:
            _held.pop(path, None)
            _unlock(fd, *region)
    finally:
        os.close(fd)


@contextlib.contextmanager
def coordinator_lock(timeout=5.0):
    """Exclusive single-coordinator lock; raises if another worker is running."""
    path = _lock_path("worker.lock")
    fd = _open(path)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd, True, 0, 1):
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "another clipshelf worker coordinator is already running "
                    f"(lock: {path})"
                )
            time.sleep(POLL)
        try:
            yield
        finally:
            _unlock(fd, 0, 1)
    finally:
        os.close(fd)
