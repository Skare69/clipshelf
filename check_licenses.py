#!/usr/bin/env python3
"""Third-party license gate (stdlib only).

Enforces license_inventory.toml against reality:
  python  : the installed set (the image ships exactly `pip install
            -r requirements.txt`) must equal the inventoried set for this
            platform, and every entry's license must be allowlisted and
            unchanged since review.
  android : the gradle-resolved releaseRuntimeClasspath must equal the
            inventoried artifact set (exact versions).
  image   : Dockerfile pins (base, apt, SQLite) must match the inventory, and
            the shipped THIRD_PARTY_NOTICES.md must exist and be byte-identical
            to the copy packed into the APK assets.

Usage:
  check_licenses.py                 # python + image + notices (CI python job, local)
  check_licenses.py --site DIR      # python set from a --target install dir
  check_licenses.py android --deps FILE   # CI android job (after gradlew dependencies)
"""
import argparse
import re
import sys
import tomllib
from importlib.metadata import distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INV = tomllib.loads((ROOT / "license_inventory.toml").read_text(encoding="utf-8"))

errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def norm(name: str) -> str:
    return name.lower().replace("_", "-")


def raw_license(dist) -> str:
    md = dist.metadata
    return (
        md.get("License-Expression")
        or md.get("License")
        or "; ".join(
            c.split(":: ")[-1]
            for c in (md.get_all("Classifier") or [])
            if c.startswith("License")
        )
    ).strip()


def check_notices() -> None:
    root = ROOT / "THIRD_PARTY_NOTICES.md"
    apk = ROOT / "android/app/src/main/assets/THIRD_PARTY_NOTICES.md"
    if not root.is_file() or not apk.is_file():
        fail(f"notices: missing {root} or {apk}")
        return
    if root.read_bytes() != apk.read_bytes():
        fail("notices: android/app/src/main/assets/THIRD_PARTY_NOTICES.md "
             "differs from the root copy")


def check_python(site: str | None) -> None:
    tooling = set(INV["allow"]["tooling"])
    kwargs = {"path": [site]} if site else {}
    installed = {}
    for d in distributions(**kwargs):
        name = norm(d.metadata["Name"])
        if name in tooling:
            continue
        installed[name] = d

    here = sys.platform  # "win32" / "linux"
    entries = INV["python"]["entries"]
    expected = {
        n: e for n, e in entries.items()
        if here in (e.get("platforms") or ["win32", "linux"])
    }

    for n in sorted(set(expected) - set(installed)):
        fail(f"python: {n} is inventoried but not installed")
    for n in sorted(set(installed) - set(expected)):
        fail(f"python: installed {n}=={installed[n].version} is not in "
             "license_inventory.toml — review its license, then add it")
    for n in sorted(set(expected) & set(installed)):
        entry, dist = expected[n], installed[n]
        if entry["license"] not in INV["allow"]["ids"]:
            fail(f"python: {n} license {entry['license']} is not allowlisted")
        raw = raw_license(dist)
        if raw != entry["meta"]:
            fail(f"python: {n} license metadata changed since review: "
                 f"{raw!r} != {entry['meta']!r} — re-review its license")


def check_android(deps_file: str) -> None:
    text = Path(deps_file).read_text(encoding="utf-8-sig", errors="replace")
    found = {}

    def clean(s: str) -> str:
        s = s.strip()
        while True:
            if s.endswith(("(c)", "(*)")):
                s = s[:-3].strip()
            elif s.endswith("!"):  # "1.0!!" = forced
                s = s.rstrip("!").strip()
            else:
                return s

    for n, line in enumerate(text.splitlines(), 1):
        _, sep, rest = line.rpartition("--- ")
        if not sep:
            if line.lstrip().startswith(("+---", "\\---")):
                fail(f"android: line {n}: unparseable dependency node {line.strip()!r}")
            continue
        spec = rest.strip()
        if "(c)" in spec and not spec.endswith("(c)"):  # embedded marker, not a suffix
            fail(f"android: line {n}: unparseable dependency node {spec!r}")
            continue
        constraint = spec.endswith("(c)")  # constraint node, not packaged
        if constraint:
            spec = spec[:-3].strip()
        # "g:a:v", "g:a:v -> selected", or BOM-style "g:a -> selected"
        left, arrow, right = spec.partition("->")
        parts = left.strip().split(":")
        tok = r"[0-9A-Za-z][\w.+-]*"  # valid Gradle coordinate token
        good = (len(parts) in ((2, 3) if arrow else (3,)) and all(parts)
                and all(re.fullmatch(tok, p) for p in parts[:2]))
        if arrow:
            if not good or not (v := clean(right)):
                fail(f"android: line {n}: unparseable dependency node {spec!r}")
                continue
        else:
            if not good or not (v := clean(parts[2])):
                fail(f"android: line {n}: unparseable dependency node {spec!r}")
                continue
        if not re.fullmatch(r"[0-9A-Za-z][\w.+-]*", v):
            fail(f"android: line {n}: unparseable version {v!r} in {spec!r}")
            continue
        if constraint:
            continue  # valid coordinate + constraint suffix, not packaged
        key = f"{parts[0]}:{parts[1]}"
        if key in INV["android"]["non_packaged"]:
            continue  # metadata-only, not packaged in the APK
        if key not in found:  # (*) = subtree shown elsewhere; version still real
            found[key] = v

    entries = INV["android"]["entries"]
    for key in sorted(set(entries) - set(found)):
        fail(f"android: {key} is inventoried but not resolved")
    for key in sorted(set(found) - set(entries)):
        fail(f"android: resolved {key}:{found[key]} is not in "
             "license_inventory.toml — review its license, then add it")
    for key in sorted(set(entries) & set(found)):
        entry = entries[key]
        if entry["license"] not in INV["allow"]["ids"]:
            fail(f"android: {key} license {entry['license']} is not allowlisted")
        if entry["observed"] != found[key]:
            fail(f"android: {key} version changed: resolved {found[key]}, "
                 f"reviewed {entry['observed']} — re-review, then update")


def check_image() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    img = INV["image"]

    stage_aliases = set()
    bases = []
    for match in re.finditer(
            r"^[ \t]*FROM[ \t]+(?:(?:--\S+)[ \t]+)*(\S+)(?:[ \t]+AS[ \t]+(\S+))?",
            text, re.M | re.I):
        image, alias = match.groups()
        if image.casefold() not in stage_aliases:
            bases.append(image)
        if alias:
            stage_aliases.add(alias.casefold())

    if not bases:
        fail(f"image: base image is missing, inventory pins {img['base']}")
    for base in bases:
        if base != img["base"]:
            fail(f"image: base image is {base}, inventory pins {img['base']}")

    stages = [sorted(m.group(1).split())
              for m in re.finditer(r"apt-get install -y --no-install-recommends ([^\\]+)", text)]
    want = [sorted(img["apt_build_stage"]), sorted(img["apt_runtime"])]
    if stages != want:
        fail(f"image: apt stage sets {stages} != inventoried {want}")

    for key, arg in [("version", "SQLITE_VERSION"), ("tarball", "SQLITE_TARBALL"),
                     ("url", "SQLITE_URL"), ("sha256", "SQLITE_SHA256")]:
        m = re.search(rf"ARG {arg}=(\S+)", text)
        if not m or m.group(1) != img["sqlite"][key]:
            fail(f"image: ARG {arg} is {m.group(1) if m else 'missing'}, "
                 f"inventory pins {img['sqlite'][key]}")

    if not re.search(r"^COPY THIRD_PARTY_NOTICES\.md", text, re.M):
        fail("image: Dockerfile does not COPY THIRD_PARTY_NOTICES.md")


def main() -> int:
    ap = argparse.ArgumentParser(description="Third-party license gate")
    ap.add_argument("--site", help="pip --target dir to inspect instead of the env")
    ap.add_argument("mode", nargs="?", choices=["python", "android", "image"],
                    help="run one mode (default: python + image + notices)")
    ap.add_argument("--deps", help="gradle dependencies output for android mode")
    args = ap.parse_args()

    if args.mode in ("python", None):
        check_python(args.site)
    if args.mode == "android":
        if not args.deps:
            ap.error("android mode needs --deps FILE")
        check_android(args.deps)
    if args.mode in ("image", None):
        check_image()
    check_notices()

    if errors:
        for e in errors:
            print(f"LICENSE GATE: {e}", file=sys.stderr)
        print(f"license gate: {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print("license gate: OK "
          f"(python {len(INV['python']['entries'])}, "
          f"android {len(INV['android']['entries'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
