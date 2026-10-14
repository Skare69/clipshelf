"""Regression: docker-publish must derive every image tag from the validated
version, never from the raw GITHUB_REF_NAME (v0.11.0 release-review finding).

On workflow_dispatch GITHUB_REF_NAME is the starting branch, so publishing it
raw produced a mutable branch tag from main, an invalid image reference from
`ott/<task-id>-<slug>` branches, and let any branch named `latest` overwrite
that tag. Simulates the "Resolve versioned tags" step in bash with the same
env the runner provides and asserts the emitted tags.
"""
import os
import re
import subprocess
import tempfile

WORKFLOW = os.path.join(os.path.dirname(__file__), ".github/workflows/docker-publish.yml")
# ${{ github.repository }} with mixed case, to also prove the lowercasing.
IMAGE = "ghcr.io/skare69/clipshelf"
# Distribution-reference-shaped tag components only: v-prefixed semver, full
# semver, or major.minor. A branch name (with or without a slash) never matches.
TAG_RE = re.compile(r"(v?\d+\.\d+\.\d+|\d+\.\d+)")


def step_script():
    lines = open(WORKFLOW).read().splitlines()
    start = next(i for i, l in enumerate(lines) if "name: Resolve versioned tags" in l)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:])
    return "\n".join(body).replace("${{ github.repository }}", "Skare69/Clipshelf")


def resolve_tags(ref, version_input):
    with tempfile.TemporaryDirectory() as tmp:
        output = os.path.join(tmp, "github_output")
        env = dict(os.environ, GITHUB_REF_NAME=ref, VERSION_INPUT=version_input,
                   GITHUB_OUTPUT=output)
        subprocess.run(["bash", "-c", step_script()], env=env, check=True,
                       capture_output=True, text=True)
        text = open(output).read()
    return text.split("tags<<TAGS\n", 1)[1].split("\nTAGS", 1)[0].splitlines()


def check(name, ref, version_input, expected):
    tags = resolve_tags(ref, version_input)
    for tag in tags:
        assert tag.startswith(IMAGE + ":"), (name, "not the test image", tag)
        assert TAG_RE.fullmatch(tag.split(":", 1)[1]), (name, "invalid reference", tag)
    expected_tags = [f"{IMAGE}:{t}" for t in expected]
    assert tags == expected_tags, (name, tags, expected_tags)
    print(f"ok: {name}: {' '.join(tags)}")


# The acceptance cases: dispatch from main, and a v* tag push.
check("dispatch from main", "main", "0.11.1", ["0.11.1", "0.11", "v0.11.1"])
check("tag push", "v0.11.0", "", ["0.11.0", "0.11", "v0.11.0"])
# Branch shapes that made the raw GITHUB_REF_NAME invalid or hostile.
check("dispatch from task branch", "ott/1a2b3c4d-fix-thing", "0.11.1",
      ["0.11.1", "0.11", "v0.11.1"])
check("branch named like a tag", "latest", "0.10.0", ["0.10.0", "0.10", "v0.10.0"])
print("ok: every publish tag derives from the validated version")
