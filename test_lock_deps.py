import importlib.util
import subprocess
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
            with patch.object(lock_deps, "resolve", return_value=dict(PINS)), \
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


if __name__ == "__main__":
    unittest.main()
