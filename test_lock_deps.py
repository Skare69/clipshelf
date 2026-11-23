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
        py = cmd[cmd.index("--python-version") + 1]
        cp = f"cp{py.replace('.', '')}"
        for n, v in PINS.items():
            if broken and n == "other-pkg":
                continue
            (dest / f"{n.replace('-', '_')}-{v}-{cp}-{cp}-{tag}.whl").write_bytes(
                f"{py}:{tag}".encode())
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
            # one distinct wheel hash per (python, platform) set
            self.assertEqual(len(hashes), len(lock_deps.PY_PLATFORM_SETS))

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
    REQ = 'demo>=0.9\ntzdata; sys_platform == "win32"\n'

    def check(self, report_items=None, req_text=None, lock_text=None):
        """Run check() with resolve patched to raise (check must never resolve)
        and each per-platform pip install succeeding while writing the given
        report items; returns (code, output, resolve mock)."""
        if report_items is None:
            report_items = [
                {"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}},
                {"is_yanked": False, "metadata": {"name": "tzdata", "version": "2026.5"}},
            ]
        req_text = req_text or self.REQ
        lock_text = lock_text or self.LOCK
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "requirements.txt"
            req.write_text(req_text, encoding="utf-8")
            lock = Path(d) / "requirements.lock"
            lock.write_text(lock_text, encoding="utf-8")

            def run(cmd, **kwargs):
                report = Path(cmd[cmd.index("--report") + 1])
                report.write_text(json.dumps({"install": report_items}),
                                  encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            out = io.StringIO()
            with patch.object(lock_deps, "resolve",
                              side_effect=AssertionError("live resolution in check")) as res_mock, \
                    patch.object(lock_deps.subprocess, "run", side_effect=run), \
                    contextlib.redirect_stdout(out):
                code = lock_deps.check("python", req, lock)
            return code, out.getvalue(), res_mock

    def test_passes_when_upstream_shipped_a_newer_patch(self):
        # the point of the split: a patch released after the merge cannot fail
        # the deterministic gate, because check never resolves
        code, out, res_mock = self.check(
            [{"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}}],
            req_text="demo>=0.9\n",
            lock_text="demo==1.0 \\\n    --hash=sha256:aaaa\n")
        self.assertEqual(code, 0)
        self.assertIn("ok: 1 pins cover requirements.txt", out)
        res_mock.assert_not_called()

    def test_missing_requirement_fails(self):
        code, out, _ = self.check(
            req_text="demo>=0.9\nother-pkg>=1\n",
            lock_text="demo==1.0 \\\n    --hash=sha256:aaaa\n")
        self.assertEqual(code, 1)
        self.assertIn("DRIFT:", out)
        self.assertIn("missing from lock: other-pkg", out)
        self.assertNotIn("suggested subject", out)

    def test_unparseable_requirement_line_fails(self):
        code, out, _ = self.check(
            req_text="demo @ https://example.invalid/demo.tar.gz\n")
        self.assertEqual(code, 1)
        self.assertIn("DRIFT:", out)
        self.assertIn("unparseable requirement line: "
                      "demo @ https://example.invalid/demo.tar.gz", out)

    def test_pin_without_hash_fails(self):
        code, out, _ = self.check(req_text="demo>=0.9\n",
                                  lock_text="demo==1.0\n")
        self.assertEqual(code, 1)
        self.assertIn("DRIFT:", out)
        self.assertIn("no hash for pin: demo", out)

    def test_yanked_pin_in_platform_report_fails(self):
        # the carried win32-only tzdata pin: yanked upstream, invisible to the
        # Linux resolution, visible only in the per-platform pip report
        code, out, _ = self.check([
            {"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}},
            {"is_yanked": True, "metadata": {"name": "tzdata", "version": "2026.5"}},
        ])
        self.assertEqual(code, 1)
        self.assertIn("yanked: tzdata==2026.5", out)

    def test_clean_platform_report_passes(self):
        code, out, res_mock = self.check([
            {"is_yanked": False, "metadata": {"name": "demo", "version": "1.0"}},
            {"is_yanked": False, "metadata": {"name": "tzdata", "version": "2026.5"}},
        ])
        self.assertEqual(code, 0)
        self.assertIn("ok: 2 pins cover requirements.txt", out)
        res_mock.assert_not_called()


class DriftTests(unittest.TestCase):
    LOCK = ("demo==1.0 \\\n"
            "    --hash=sha256:aaaa\n"
            "tzdata==2026.5 \\\n"
            "    --hash=sha256:bbbb\n")
    DJANGO_LOCK = ("django==6.1.3 \\\n"
                   "    --hash=sha256:cccc\n"
                   "tzdata==2026.5 \\\n"
                   "    --hash=sha256:bbbb\n")

    def drift(self, pins, req_text="demo>=0.9\n", lock_text=None, gated=None):
        """Run drift() with resolve patched to the given live resolution and
        subprocess patched to raise (drift runs no pip)."""
        lock_text = lock_text or self.LOCK
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "requirements.txt"
            req.write_text(req_text, encoding="utf-8")
            lock = Path(d) / "requirements.lock"
            lock.write_text(lock_text, encoding="utf-8")
            out = io.StringIO()
            with patch.object(lock_deps, "resolve", return_value=lock_deps.Resolution(
                    pins=dict(pins), yanked=set(),
                    gated=dict(pins if gated is None else gated))), \
                    patch.object(lock_deps.subprocess, "run",
                                 side_effect=AssertionError("live pip in drift")), \
                    contextlib.redirect_stdout(out):
                code = lock_deps.drift("python", req, lock)
            return code, out.getvalue()

    def test_changed_pin_reports_suggested_subject(self):
        # bump(): 1.0 -> 1.1 moves the second segment, so the kind is "minor"
        code, out = self.drift(
            {"demo": "1.1", "tzdata": "2026.5"},
            gated={"demo": "1.0", "tzdata": "2026.5"})
        self.assertEqual(code, 1)
        self.assertIn("DRIFT:", out)
        self.assertIn("changed: demo lock=1.0 current=1.1", out)
        self.assertIn("suggested subject: fix: refresh the dependency lock "
                      "(demo 1.0->1.1 minor)", out)

    def test_clean_resolution_passes(self):
        code, out = self.drift({"demo": "1.0", "tzdata": "2026.5"})
        self.assertEqual(code, 0)
        self.assertNotIn("DRIFT:", out)

    def test_carried_tzdata_warns_and_passes(self):
        # the win32-only Django dep: absent from the Linux resolution, kept in
        # the shipped lock as a warning, never a failure
        code, out = self.drift({"django": "6.1.3"}, req_text="django>=5.0\n",
                               lock_text=self.DJANGO_LOCK)
        self.assertEqual(code, 0)
        self.assertIn("warning: extra in lock: tzdata", out)


if __name__ == "__main__":
    unittest.main()
