import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "lock_deps", Path(__file__).parent / "scripts" / "lock_deps.py")
lock_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_deps)

PINS = {"demo": "1.0", "other-pkg": "2.0"}


def fake_pip(broken_platform, mode):
    """subprocess.run stand-in for `pip download`: one platform fails or skips a wheel."""
    def run(cmd, **kwargs):
        dest = Path(cmd[cmd.index("-d") + 1])
        dest.mkdir(parents=True, exist_ok=True)
        broken = broken_platform in cmd
        if broken and mode == "fail":
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: no matching distribution")
        tag = "-".join(p for i, p in enumerate(cmd) if cmd[i - 1] == "--platform")
        for n, v in PINS.items():
            if broken and n == "other-pkg":
                continue
            (dest / f"{n.replace('-', '_')}-{v}-cp313-cp313-{tag}.whl").write_bytes(tag.encode())
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


class MakeTests(unittest.TestCase):
    def make(self, run):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "requirements.lock"
            with patch.object(lock_deps, "resolve", return_value=lock_deps.Resolution(
                    pins=dict(PINS), yanked=set(), gated=dict(PINS))), \
                    patch.object(lock_deps.subprocess, "run", side_effect=run):
                try:
                    lock_deps.make("python", Path("requirements.txt"), out)
                except SystemExit as e:
                    return None, str(e.code)
            return out.read_text(encoding="utf-8"), None

    def test_any_platform_without_wheels_aborts_before_writing(self):
        for mode in ("fail", "skip"):
            with self.subTest(mode=mode):
                lock, err = self.make(fake_pip("manylinux2014_aarch64", mode))
                self.assertIsNone(lock)
                self.assertIn("aarch64", err)
                self.assertIn("lock not written", err)

    def test_all_platforms_hash_every_pin(self):
        lock, err = self.make(fake_pip("no-such-platform", "fail"))
        self.assertIsNone(err)
        lines = lock.splitlines()
        for n, v in PINS.items():
            start = lines.index(f"{n}=={v} \\") + 1
            hashes = []
            while start < len(lines) and lines[start].lstrip().startswith("--hash=sha256:"):
                hashes.append(lines[start])
                start += 1
            # one distinct wheel hash per platform set
            self.assertEqual(len(hashes), len(lock_deps.PLATFORM_SETS))

    def test_reports_pin_count_on_stdout(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lock, err = self.make(fake_pip("no-such-platform", "fail"))
        self.assertIsNone(err)
        wrote = out.getvalue().splitlines()[1]
        self.assertTrue(wrote.startswith("wrote ") and wrote.endswith(" with 2 pins"),
                        wrote)

    def test_main_uses_default_out(self):
        calls = []
        with patch.object(lock_deps, "make",
                          lambda *a, **k: calls.append((a, k))), \
                patch.object(lock_deps.sys, "argv", ["lock_deps.py", "make"]):
            lock_deps.main()
        self.assertEqual(calls[0][0],
                         (sys.executable, Path("requirements.txt"),
                          Path("requirements.lock"), None))


class DryRunTests(unittest.TestCase):
    REPORT = [{"is_yanked": True, "metadata": {"name": "Demo_Pkg", "version": "1.2.3"}},
              {"metadata": {"name": "other-pkg", "version": "2.0"}}]

    def test_dry_run(self):
        cmds = []
        with tempfile.TemporaryDirectory() as d:
            def run(cmd, **kwargs):
                cmds.append(cmd)
                report = Path(cmd[cmd.index("--report") + 1])
                report.write_text(json.dumps({"install": self.REPORT}), encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with patch.object(lock_deps.subprocess, "run", side_effect=run):
                pins, yanked = lock_deps.dry_run("python", Path("requirements.txt"),
                                                 Path(d), [])

        self.assertEqual(cmds, [["python", "-m", "pip", "install", "--dry-run",
                                 "--report", str(Path(d) / "report.json"),
                                 "--ignore-installed", "-r", "requirements.txt"]])
        self.assertEqual(pins, {"demo-pkg": "1.2.3", "other-pkg": "2.0"})
        self.assertEqual(yanked, {"demo-pkg"})


class CheckTests(unittest.TestCase):
    LOCK = ("demo==1.0 \\\n"
            "    --hash=sha256:aaaa\n"
            "tzdata==2026.5 \\\n"
            "    --hash=sha256:bbbb\n")

    def check(self, report_items):
        """Run check() with resolve patched and each per-platform pip install
        succeeding while writing the given report items."""
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "requirements.lock"
            lock.write_text(self.LOCK, encoding="utf-8")

            def run(cmd, **kwargs):
                report = Path(cmd[cmd.index("--report") + 1])
                report.write_text(json.dumps({"install": report_items}),
                                  encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            out = io.StringIO()
            with patch.object(lock_deps, "resolve", return_value=lock_deps.Resolution(
                    pins={"demo": "1.0", "tzdata": "2026.5"}, yanked=set(),
                    gated={"demo": "1.0", "tzdata": "2026.5"})), \
                    patch.object(lock_deps.subprocess, "run", side_effect=run), \
                    contextlib.redirect_stdout(out):
                code = lock_deps.check("python", Path("requirements.txt"), lock)
            return code, out.getvalue()

    def test_yanked_pin_in_platform_report_fails(self):
        # the carried win32-only tzdata pin: yanked upstream, invisible to the
        # Linux resolution, visible only in the per-platform pip report
        code, out = self.check([
            {"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}},
            {"is_yanked": True, "metadata": {"name": "tzdata", "version": "2026.5"}},
        ])
        self.assertEqual(code, 1)
        self.assertIn("yanked: tzdata==2026.5", out)

    def test_clean_platform_report_passes(self):
        code, out = self.check([
            {"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}},
            {"is_yanked": False, "metadata": {"name": "tzdata", "version": "2026.5"}},
        ])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
