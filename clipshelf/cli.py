"""clipshelf entry: standard Django management commands plus Waitress serve.

Usage:
  python clipshelf.py <django-command> [args...]
      # check, migrate, worker [--once], bootstrap_admin --email EMAIL,
      # import_library --user EMAIL --input library.json, test, shell, ...
  python clipshelf.py serve [host][:port]
      # one-process production Waitress server (default 127.0.0.1:8000)

serve and ordinary commands hold the shared data lock; a command that takes
the exclusive one itself declares owns_data_lock = True on its Command class;
check/test run unlocked.
"""
import os
import time
from contextlib import nullcontext


def _bind(arg):
    """'host:port', 'host', ':port', '[v6]:port' -> (host, port); local default."""
    host, sep, port = arg.rpartition(":")
    if not sep:
        host, port = arg, ""
    try:
        port = int(port) if port else 8000
    except ValueError:
        raise SystemExit(f"clipshelf serve: bad host:port {arg!r}")
    if not 1 <= port <= 65535:
        raise SystemExit(f"clipshelf serve: port {port} out of range")
    return host.strip("[]") or "127.0.0.1", port


def _migrate():
    """Bring the schema up before a long-running role starts.

    A fresh deployment starts with an empty data directory; web and worker
    apply migrations themselves so a first run needs no operator command.
    """
    from django.core.management import call_command
    from django.db import OperationalError
    from clipshelf.management.locks import data_lock

    with data_lock():
        # ponytail: web and worker may start at the same moment against one
        # SQLite file; the one that loses the write lock retries instead of
        # coordinating. Five tries covers two local processes — if a deployment
        # ever starts more, take an flock in DATA_DIR instead.
        for attempt in range(5):
            try:
                return call_command("migrate", verbosity=0)
            except OperationalError:
                if attempt == 4:
                    raise
                time.sleep(2)


def _serve(host, port):
    from clipshelf.management.locks import data_lock
    from clipshelf.project.wsgi import application
    from waitress import serve
    with data_lock():  # shared: a concurrent backup must not snapshot mid-serve
        print(f"clipshelf on http://{host}:{port} (Ctrl+C stops)")
        # channel_timeout covers the slowest admin request: the model capability
        # check talks to the configured endpoint and a local model can be slow.
        serve(application, host=host, port=port, channel_timeout=900)


def main(argv):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")
    import django
    django.setup()
    if argv[:1] == ["serve"]:
        _migrate()
        return _serve(*_bind(argv[1] if len(argv) > 1 else ""))
    if argv[:1] == ["worker"]:
        _migrate()
    # check/test need no lock. A command that takes the exclusive data lock
    # itself declares owns_data_lock; wrapping it in the shared lock would
    # die on the nesting RuntimeError. Unknown commands keep the shared lock.
    name = argv[0] if argv else ""
    if name in ("check", "test"):
        lock = nullcontext()
    else:
        from django.core.management import get_commands, load_command_class
        app = get_commands().get(name)
        if app is not None and getattr(load_command_class(app, name), "owns_data_lock", False):
            lock = nullcontext()
        else:
            from clipshelf.management.locks import data_lock
            lock = data_lock()
    with lock:
        from django.core.management import execute_from_command_line
        execute_from_command_line(["clipshelf.py", *argv])
