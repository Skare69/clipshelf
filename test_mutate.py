import subprocess
import tempfile
import unittest
from pathlib import Path

import mutate

# Gitignored personal/local entries a real checkout carries; none may reach
# the mutation workspace.
IGNORED = [
    "cache/thumb.jpg", "installs/app.apk", "tiktok.json", "links.txt",
    "whatsapp.txt", "chunks/0001.json", "_interp/batch.json", "release.jks",
    "android/debug.keystore", ".env", ".secrets/token", "data/db.sqlite3",
    "docs/notes.md", "library.json", "server.log", "pkg/__pycache__/mod.pyc",
]


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class CopyTrackedTests(unittest.TestCase):
    def test_copies_tracked_working_tree_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, ws = Path(tmp) / "repo", Path(tmp) / "ws"
            repo.mkdir()
            git(repo, "init", "-q")
            (repo / ".gitignore").write_bytes(
                (Path(__file__).parent / ".gitignore").read_bytes())
            (repo / "pkg").mkdir()
            (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "top.py").write_text("y = 2\n", encoding="utf-8")
            for rel in IGNORED:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text("sentinel\n", encoding="utf-8")
            git(repo, "add", "-A")
            (repo / "untracked.txt").write_text("sentinel\n", encoding="utf-8")
            (repo / "top.py").write_text("y = 3\n", encoding="utf-8")  # unstaged edit

            copied = mutate.copy_tracked(repo, ws)

            on_disk = {p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()}
            self.assertEqual(on_disk, {".gitignore", "pkg/mod.py", "top.py"})
            self.assertEqual(copied, on_disk)
            self.assertEqual((ws / "top.py").read_text(encoding="utf-8"), "y = 3\n")


if __name__ == "__main__":
    unittest.main()
