"""Container image structure tests (finding G5), plus deploy probe semantics.

Compilers (gcc/make/libc6-dev) belong only to the discarded SQLite build
stage; the runtime image ships just the built shared library. The SQLite
library version itself and the in-image verify job (docker-publish /
compose ops `verify` profile) stay image-bound; the deploy scripts' probe
semantics — unique probe files, no user-file deletion, live-WAL read
ordering, collision freedom — are exercised below on temp directories.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
DEPLOY = DOCKERFILE.parent / "deploy"

BUILD_TOOLS = ("gcc", "libc6-dev", "make")


def parse_stages(text):
    """Map each FROM stage name to its normalized instruction lines."""
    stages, current = {}, None
    for line in text.splitlines():
        parts = line.strip().split()
        if parts and parts[0].upper() == "FROM":
            current = parts[-1].lower()
            stages[current] = []
        elif parts and current is not None:
            stages[current].append(" ".join(parts))
    return stages


def apt_line(stage):
    return " ".join(line for line in stage if "apt-get install" in line)


class ContainerImageTests(SimpleTestCase):
    def test_build_tools_exist_only_in_discarded_stage(self):
        stages = parse_stages(DOCKERFILE.read_text(encoding="utf-8"))
        names = list(stages)
        runtime_pkgs = apt_line(stages[names[-1]])
        for tool in BUILD_TOOLS + ("build-essential",):
            self.assertNotIn(tool, runtime_pkgs)
        # the shipped stage still gets its real runtime packages
        self.assertIn("ffmpeg", runtime_pkgs)
        self.assertIn("ca-certificates", runtime_pkgs)
        builders = [
            s for s in (stages[n] for n in names[:-1])
            if all(t in apt_line(s) for t in BUILD_TOOLS)
        ]
        self.assertEqual(len(builders), 1)

    def test_runtime_stage_ships_built_sqlite_library(self):
        stages = parse_stages(DOCKERFILE.read_text(encoding="utf-8"))
        runtime = "\n".join(stages[list(stages)[-1]])
        self.assertIn("COPY --from=", runtime)
        self.assertIn("libsqlite3.so", runtime)
        self.assertIn("ENV LD_LIBRARY_PATH=/usr/local/lib", runtime)


def load_deploy_script(name, unique):
    """Load a deploy/* standalone script as a throwaway module copy."""
    spec = importlib.util.spec_from_file_location(
        f"clipshelf._deploy_probe_{unique}", DEPLOY / name
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def temp_data_dir(testcase):
    data_dir = tempfile.mkdtemp(prefix="clipshelf-probe-test-")
    testcase.addCleanup(shutil.rmtree, data_dir, ignore_errors=True)
    return data_dir


class _SpyConnection:
    """Forwarding proxy that records close() ordering for connect spies."""

    def __init__(self, inner, events):
        self._inner, self._events = inner, events

    def execute(self, sql, *args):
        return self._inner.execute(sql, *args)

    def commit(self):
        self._inner.commit()

    def close(self):
        self._events.append("close")
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def run_sqlite_probe(testcase, data_dir, connect_spy=None):
    """Run deploy/check_sqlite.py main() against a temp data dir.

    The version gate is bypassed with a fake high version so the test does
    not depend on the local SQLite build; the probe itself still runs the
    real local sqlite3 library. Returns captured stdout.
    """
    mod = load_deploy_script("check_sqlite.py", "check_sqlite")
    out = io.StringIO()
    stack = [
        mock.patch.dict(os.environ, {"CLIPSHELF_DATA_DIR": data_dir}),
        mock.patch.object(sqlite3, "sqlite_version", "9.9.9"),
    ]
    if connect_spy is not None:
        stack.append(mock.patch.object(sqlite3, "connect", side_effect=connect_spy))
    with contextlib.ExitStack() as ctx:
        for patch in stack:
            ctx.enter_context(patch)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            mod.main()
    return out.getvalue()


class DeployProbeTests(SimpleTestCase):
    def test_sqlite_probe_never_touches_preexisting_user_files(self):
        # A user database that happens to carry the old fixed probe name must
        # survive a verify run byte-for-byte, including its -wal/-shm sidecars.
        data_dir = temp_data_dir(self)
        user_db = os.path.join(data_dir, "sqlite-verification.db")
        con = sqlite3.connect(user_db)
        con.execute("CREATE TABLE user_data(x INTEGER)")
        con.execute("INSERT INTO user_data VALUES (42)")
        con.commit()
        con.close()
        Path(user_db + "-wal").write_bytes(b"user wal bytes")
        Path(user_db + "-shm").write_bytes(b"user shm bytes")
        before = {p: Path(p).read_bytes() for p in
                  (user_db, user_db + "-wal", user_db + "-shm")}

        out = run_sqlite_probe(self, data_dir)

        for path, data in before.items():
            self.assertEqual(Path(path).read_bytes(), data, path)
        self.assertIn("PASS", out)

    def test_sqlite_probe_reads_wal_while_writer_still_open(self):
        # The fresh reader must connect and read before the writer closes:
        # once the last connection closes, SQLite checkpoints and removes the
        # WAL, so reading afterwards proves nothing about live-WAL visibility.
        data_dir = temp_data_dir(self)
        events = []
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            events.append("open")
            return _SpyConnection(real_connect(*args, **kwargs), events)

        out = run_sqlite_probe(self, data_dir, connect_spy=spy_connect)

        self.assertEqual(events, ["open", "open", "close", "close"], events)
        self.assertIn("PASS", out)

    def test_sqlite_probe_success_message_does_not_claim_django_pragmas(self):
        # The probe exercises raw sqlite3 connections only; it must not claim
        # to have proven anything about Django connection pragmas.
        data_dir = temp_data_dir(self)
        out = run_sqlite_probe(self, data_dir)
        self.assertIn("PASS", out)
        self.assertNotIn("App settings enforce", out)

    def test_entrypoint_probe_tolerates_preexisting_probe_file(self):
        # A pre-existing file with the historical probe name is user data:
        # startup must succeed and leave it untouched.
        mod = load_deploy_script("entrypoint.py", "entrypoint")
        data_dir = temp_data_dir(self)
        sentinel = os.path.join(data_dir, ".entrypoint-write-probe")
        Path(sentinel).write_text("operator data", encoding="ascii")
        mod.DATA_DIR = data_dir

        mod.ensure_data_dir()

        self.assertEqual(Path(sentinel).read_text(encoding="ascii"), "operator data")

    def test_entrypoint_probe_is_collision_free_under_simultaneous_starts(self):
        # web and worker start at the same time; both write probes must
        # succeed, and neither may leave a probe file behind.
        mod = load_deploy_script("entrypoint.py", "entrypoint2")
        data_dir = temp_data_dir(self)
        mod.DATA_DIR = data_dir
        barrier = threading.Barrier(2)
        errors = []

        def start():
            barrier.wait()
            try:
                mod.ensure_data_dir()
            except BaseException as exc:  # noqa: BLE001 - collected below
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            [n for n in os.listdir(data_dir) if n.startswith(".entrypoint-write-probe")],
            [],
        )
