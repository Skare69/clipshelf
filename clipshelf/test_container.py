"""Container image structure tests (finding G5).

Compilers (gcc/make/libc6-dev) belong only to the discarded SQLite build
stage; the runtime image ships just the built shared library. The runtime
behavior itself — WAL fix, journal mode, durability — is proven inside the
real image by deploy/check_sqlite.py (docker-publish verify job / compose
ops `verify` profile), which this environment cannot run.
"""

from pathlib import Path

from django.test import SimpleTestCase

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

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
