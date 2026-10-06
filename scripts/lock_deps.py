#!/usr/bin/env python3
"""Hash-locked dependency resolution shared by CI and Docker.

Usage:
  python scripts/lock_deps.py make [requirements.txt] [-o requirements.lock]
  python scripts/lock_deps.py check [requirements.txt] [requirements.lock]
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

PLATFORM_SETS = [
    ["win_amd64"],
    ["manylinux2014_x86_64", "manylinux_2_28_x86_64"],
    ["manylinux2014_aarch64", "manylinux_2_28_aarch64"],
]

HEADER = """\
# requirements.lock — exact hash-locked closure of requirements.txt.
# Purpose: single dependency resolution shared by CI and Docker.
# Regenerate: python scripts/lock_deps.py make
# Policy: regenerate whenever requirements.txt changes or `check` fails.
# Platforms covered: win_amd64, manylinux x86_64/aarch64. Python 3.13 (cp313/abi3).
# Date: {date}
"""


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve(python: str, req: Path, tmp: Path) -> dict[str, str]:
    """Dry-run install, return {canonical_name: pinned_version}."""
    report = tmp / "report.json"
    cmd = [python, "-m", "pip", "install", "--dry-run", "--report", str(report),
           "--ignore-installed", "-r", str(req)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pip dry-run failed:\n{r.stdout}\n{r.stderr}")
    pins = {}
    for item in json.loads(report.read_text(encoding="utf-8"))["install"]:
        meta = item["metadata"]
        pins[canon(meta["name"])] = meta["version"]
    return pins


def collect_hashes(python: str, pins: dict[str, str], tmp: Path) -> tuple[dict, set, set]:
    """Download wheels per platform set; return hashes per pin, dirs used, sdist-needed set."""
    pins_file = tmp / "pins.txt"
    pins_file.write_text("\n".join(f"{n}=={v}" for n, v in sorted(pins.items())) + "\n",
                         encoding="utf-8")
    hashes: dict[tuple[str, str], set] = {}
    sdists: set[str] = set()
    dirs_used: set[str] = set()

    for plats in PLATFORM_SETS:
        plat_dir = tmp / ("plat-" + "-".join(plats))
        cmd = [python, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
               "-r", str(pins_file), "-d", str(plat_dir),
               "--python-version", "3.13", "--implementation", "cp",
               "--abi", "cp313", "--abi", "abi3", "--abi", "none"]
        for p in plats:
            cmd += ["--platform", p]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[warn] wheel download failed for {plats}:\n{r.stderr}", file=sys.stderr)
            continue
        dirs_used.add(plat_dir.name)
        for f in plat_dir.iterdir():
            m = re.match(r"(.+)-([0-9][^-]*)-", f.name)
            if not m:
                continue
            n, v = canon(m.group(1)), m.group(2)
            if n not in pins or pins[n] != v:
                continue
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hashes.setdefault((n, v), set()).add(f"sha256:{h}")

    # per-package sdist fallback for anything with no wheel
    for n, v in sorted(pins.items()):
        if (n, v) in hashes:
            continue
        sdists.add(n)
        sdist_dir = tmp / "sdist" / n
        r = subprocess.run([python, "-m", "pip", "download", "--no-deps",
                            f"{n}=={v}", "-d", str(sdist_dir)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"No wheel or sdist for {n}=={v}:\n{r.stderr}")
        dirs_used.add("sdist")
        for f in sdist_dir.iterdir():
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hashes.setdefault((n, v), set()).add(f"sha256:{h}")
    return hashes, dirs_used, sdists


def make(python: str, req: Path, out: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="clipshelf-lockgen-"))
    print(f"temp: {tmp}")
    pins = resolve(python, req, tmp)
    hashes, dirs_used, sdists = collect_hashes(python, pins, tmp)

    lines = [HEADER.format(date=date.today().isoformat())]
    for n, v in sorted(pins.items()):
        hs = sorted(hashes.get((n, v), []))
        if not hs:
            continue
        lines.append(f"{n}=={v} \\")
        for j, h in enumerate(hs):
            lines.append(f"    --hash={h}" + (" \\" if j < len(hs) - 1 else ""))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {out} with {len(pins)} pins")
    for n, v in sorted(pins.items()):
        nhs = len(hashes.get((n, v), ()))
        print(f"  {n}=={v}: {nhs} hash(es)")
    print(f"platform dirs used: {sorted(dirs_used)}")
    if sdists:
        print(f"sdist fallback (no wheel): {sorted(sdists)}")


def check(python: str, req: Path, lock: Path) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="clipshelf-lockgen-check-"))
    current = resolve(python, req, tmp)
    lock_pins: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        m = re.match(r"([A-Za-z0-9_.-]+)==([^-\\\s]+)", line)
        if m:
            lock_pins[canon(m.group(1))] = m.group(2)

    problems = []
    warnings = []
    for n in sorted(set(current) | set(lock_pins)):
        cur, lk = current.get(n), lock_pins.get(n)
        if cur is None:
            # Platform-conditional deps resolve on some platforms only; the lock
            # is the cross-platform union. ponytail: allowlist is a single known
            # case (Django's win32-only tzdata); extend deliberately if another
            # marker-gated dep appears.
            if n == "tzdata" and "django" in current:
                warnings.append(f"extra in lock: tzdata=={lk} (win32-only Django dep; kept in the shipped set)")
            else:
                problems.append(f"extra in lock: {n}=={lk} not in current resolution")
        elif lk is None:
            problems.append(f"missing from lock: {n}=={cur}")
        elif cur != lk:
            problems.append(f"changed: {n} lock={lk} current={cur}")
    # every lock pin must carry at least one hash
    text = lock.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(text):
        m = re.match(r"([A-Za-z0-9_.-]+)==", line.strip())
        if not m:
            continue
        nxt = text[i + 1].strip() if i + 1 < len(text) else ""
        if not nxt.startswith("--hash="):
            problems.append(f"no hash for pin: {canon(m.group(1))}")

    if problems:
        print("DRIFT:")
        for p in problems:
            print(f"  {p}")
        return 1
    for w in warnings:
        print(f"warning: {w}")

    # end-to-end validation: pip must accept the lock for this platform
    with tempfile.TemporaryDirectory(prefix="clipshelf-lockgen-hashcheck-") as td:
        tdp = Path(td)
        r = subprocess.run(
            [python, "-m", "pip", "install", "--dry-run", "--require-hashes",
             "--report", str(tdp / "inv.json"), "-r", str(lock)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("DRIFT:")
            print("  hash validation failed for this platform")
            for ln in r.stderr.splitlines()[-15:]:
                print(f"  {ln}")
            return 1
    print(f"ok: {len(current)} pins match current resolution")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("req", nargs="?", default="requirements.txt")
    mk.add_argument("-o", "--out", default="requirements.lock")
    ck = sub.add_parser("check")
    ck.add_argument("req", nargs="?", default="requirements.txt")
    ck.add_argument("lock", nargs="?", default="requirements.lock")
    args = ap.parse_args()

    python = sys.executable
    if args.cmd == "make":
        make(python, Path(args.req), Path(args.out))
    else:
        sys.exit(check(python, Path(args.req), Path(args.lock)))


if __name__ == "__main__":
    main()
