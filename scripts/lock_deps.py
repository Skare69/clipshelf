#!/usr/bin/env python3
"""Hash-locked dependency resolution shared by CI and Docker.

Usage:
  python scripts/lock_deps.py make [requirements.txt] [-o requirements.lock] [--fresh NAME ...]
  python scripts/lock_deps.py check [requirements.txt] [requirements.lock]

Both resolve only releases uploaded at least MIN_RELEASE_AGE ago (pip >= 26.0);
the age-gated pass resolves a copy of requirements.txt without lower bounds
(exact pins kept as upper bounds), so a floor raised to a release younger
than that keeps working via `make --fresh NAME`.
A newer pin already in the lock stays, so the gate never reverts a security
bump; `make --fresh NAME` admits the newest release of NAME at once (it exits
naming the held pins to add when the fresh name does not move). Yanked pins
are never held or shipped: `check` and `make` fail on them.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

PLATFORM_SETS = [
    ["win_amd64"],
    ["manylinux2014_x86_64", "manylinux_2_28_x86_64"],
    ["manylinux2014_aarch64", "manylinux_2_28_aarch64"],
]

# Release-age gate: a new upstream release enters the lock, and turns `check`
# red, only once it is a week old.
MIN_RELEASE_AGE = timedelta(days=7)

HEADER = """\
# requirements.lock — exact hash-locked closure of requirements.txt.
# Purpose: single dependency resolution shared by CI and Docker.
# Regenerate: python scripts/lock_deps.py make
# Policy: regenerate whenever requirements.txt changes or `check` fails.
# Release age: 7+ days before the Date line; newer pins only via make --fresh.
# Platforms covered: win_amd64, manylinux x86_64/aarch64. Python 3.13 (cp313/abi3).
# Date: {date}
"""


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dry_run(python: str, req: Path, tmp: Path, extra: list[str]) -> tuple[dict[str, str], set[str]]:
    """Dry-run install; return {canonical_name: version} plus the canonical
    names pip flagged is_yanked in the report."""
    report = tmp / "report.json"
    cmd = [python, "-m", "pip", "install", "--dry-run", "--report", str(report),
           "--ignore-installed", *extra, "-r", str(req)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pip dry-run failed:\n{r.stdout}\n{r.stderr}")
    pins: dict[str, str] = {}
    yanked: set[str] = set()
    for item in json.loads(report.read_text(encoding="utf-8"))["install"]:
        meta = item["metadata"]
        pins[canon(meta["name"])] = meta["version"]
        if item.get("is_yanked"):
            yanked.add(canon(meta["name"]))
    return pins, yanked


def release(version: str) -> tuple[int, ...]:
    # ponytail: release segments only, so pre/post/dev suffixes compare equal;
    # switch to packaging.version if such a pin ever reaches the lock.
    return tuple(int(p) for p in re.match(r"\d+(?:\.\d+)*", version).group().split("."))


class Resolution(NamedTuple):
    pins: dict[str, str]   # final held-pass resolution
    yanked: set[str]       # names pip flagged is_yanked in the final pass
    gated: dict[str, str]  # pass-1 age-gated resolution


def _floor_free(line: str) -> str:
    """Drop lower-bound specifiers (>=, >) and wildcard pins (==N.*) from a
    requirement line, turning an exact pin (==V, ===V) into <=V; keeping the
    name, extras, remaining bounds and any environment marker; comments, blank
    lines and unparseable lines pass through. The age-gated pass resolves this
    copy: a floor raised to a too-young security release cannot break the
    gate, while the copy stays inside the real resolution range — an exact pin
    that vanished would let the gate hold a newer release the pin conflicts
    with."""
    if not line.strip() or line.lstrip().startswith("#") or line.lstrip().startswith("-"):
        return line
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[^\]]*\])?)\s*([^;@]*?)\s*(;.*)?$", line)
    if not m:
        return line
    name, specs, marker = m.group(1), m.group(2), m.group(3) or ""
    kept = []
    for c in specs.split(","):
        c = c.strip()
        m = re.fullmatch(r"={2,3}(.+)", c)
        if m and "*" not in m.group(1):
            # exact pin: keep it as an upper bound so the gated copy stays
            # inside the pinned range; dropped, the gate could hold a newer
            # release the real pin then conflicts with
            kept.append("<=" + m.group(1))
        elif c and not re.match(r"^(===|==|>=|>)", c):
            kept.append(c)
    return name + (" " + ",".join(kept) if kept else "") + ((" " + marker) if marker else "")


def resolve(python: str, req: Path, tmp: Path, keep: dict[str, str] | None = None,
            fresh: list[str] | None = None) -> Resolution:
    """Resolve releases older than MIN_RELEASE_AGE. Pass 1 gates on release
    age only, against a floor-free copy of requirements.txt (so a floor raised
    to a too-young release cannot break the gate). The held pass always runs
    against the real file and holds every gated pin plus every `keep` pin the
    gated pass does not know (keep-only pins would otherwise float past the
    age gate; -c constraints for unneeded names are inert). Names in `fresh`
    stay unheld. A newer pin in `keep` (the current lock) stays; yanked pins
    are unheld and re-resolved, never shipped. `--fresh` exits naming the
    held pins to add when the fresh name does not move."""
    cutoff = (datetime.now(timezone.utc) - MIN_RELEASE_AGE).strftime("%Y-%m-%dT%H:%M:%SZ")
    # pass 1: copy without lower bounds, so a young security floor
    # (e.g. Django>=5.2.19 for a release days old) still resolves under the age
    # gate; bounds like <5.3 survive, and an exact pin ==V survives as <=V,
    # keeping the copy inside the real resolution range
    gated_req = tmp / "requirements.gated.txt"
    gated_req.write_text("".join(_floor_free(l) + "\n"
                                 for l in req.read_text(encoding="utf-8").splitlines()),
                         encoding="utf-8")
    gated, _ = dry_run(python, gated_req, tmp, ["--uploaded-prior-to", cutoff])
    unheld = {canon(n) for n in fresh or ()}
    newer = {n: v for n, v in (keep or {}).items()
             if n in gated and n not in unheld and release(v) > release(gated[n])}
    held_names = {n: gated[n] for n in gated if n not in unheld}
    held_names.update((n, v) for n, v in (keep or {}).items()
                      if n not in gated and n not in unheld)
    held_names.update(newer)
    held = tmp / "held.txt"
    while True:
        held.write_text("".join(f"{n}=={v}\n" for n, v in sorted(held_names.items())),
                        encoding="utf-8")
        pins, yanked = dry_run(python, req, tmp, ["-c", str(held)] if held_names else [])
        drop = yanked & held_names.keys()
        if not drop:
            break
        # pip only installs a yanked release under an exact pin; unholding
        # settles on the age-gated non-yanked release
        for n in drop:
            del held_names[n]
    # win32-only Django marker dep: keep it in the shipped set verbatim
    if "django" in pins and "tzdata" in (keep or {}) and "tzdata" not in pins:
        # ponytail: the carried tzdata version never updates until the lock is
        # regenerated on Windows; revisit if Django ships tzdata unconditionally.
        pins["tzdata"] = keep["tzdata"]
    if unheld:
        stuck = {n for n in unheld if n not in pins
                 or (n in gated and release(pins[n]) <= release(gated[n]))}
        if stuck:
            probe, _ = dry_run(python, req, tmp, [])
            blocked = {n for n in stuck if n not in probe
                       or release(probe[n]) > release(pins.get(n, "0"))}
            if blocked:
                # a held pin keeps the fresh name on its old version
                sys.exit(f"{', '.join(f'{n}=={pins.get(n, '?')}' for n in sorted(blocked))} "
                         f"did not move (nothing newer exists otherwise): rerun adding the "
                         f"conflicting held pins, e.g. --fresh {' '.join(sorted(blocked))} "
                         f"{' '.join(sorted(held_names))}")
    return Resolution(pins, yanked, gated)


def read_pins(lock: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        m = re.match(r"([A-Za-z0-9_.-]+)==([^-\\\s]+)", line)
        if m:
            pins[canon(m.group(1))] = m.group(2)
    return pins


def plat_args(plats: list[str]) -> list[str]:
    """pip flags that select wheels for Python 3.13 on one platform set."""
    args = ["--only-binary=:all:", "--python-version", "3.13", "--implementation", "cp",
            "--abi", "cp313", "--abi", "abi3", "--abi", "none"]
    for p in plats:
        args += ["--platform", p]
    return args


def collect_hashes(python: str, pins: dict[str, str], tmp: Path) -> dict:
    """Download wheels per platform set; return hashes per pin.

    Exits when any platform set lacks a wheel for any pin: the image builds
    without a compiler, so a partial lock would only fail at publish time.
    """
    pins_file = tmp / "pins.txt"
    pins_file.write_text("\n".join(f"{n}=={v}" for n, v in sorted(pins.items())) + "\n",
                         encoding="utf-8")
    hashes: dict[tuple[str, str], set] = {}

    for plats in PLATFORM_SETS:
        plat_dir = tmp / ("plat-" + "-".join(plats))
        cmd = [python, "-m", "pip", "download", "--no-deps",
               "-r", str(pins_file), "-d", str(plat_dir)] + plat_args(plats)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"wheel download failed for {plats}; lock not written:\n{r.stderr}")
        found = set()
        for f in plat_dir.iterdir():
            m = re.match(r"(.+)-([0-9][^-]*)-", f.name)
            if not m:
                continue
            n, v = canon(m.group(1)), m.group(2)
            if n not in pins or pins[n] != v:
                continue
            found.add(n)
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hashes.setdefault((n, v), set()).add(f"sha256:{h}")
        if missing := sorted(pins.keys() - found):
            sys.exit(f"no wheel for {plats}: {missing}; lock not written")
    return hashes


def make(python: str, req: Path, out: Path, fresh: list[str] | None = None) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="clipshelf-lockgen-"))
    print(f"temp: {tmp}")
    keep = read_pins(out) if out.exists() else None
    res = resolve(python, req, tmp, keep, fresh)
    pins = res.pins
    if res.yanked:
        # pip installs yanked files under exact pins, and pip-audit ignores
        # yank status, so they would ship silently inside the lock
        y = ", ".join(f"{n}=={pins[n]}" for n in sorted(res.yanked))
        sys.exit(f"yanked: {y}; yanked files must not ship, pin an older release in requirements.txt")
    added = sorted(pins.keys() - res.gated.keys() - set(keep or {})
                   - {canon(n) for n in fresh or ()})
    if added:
        # these resolved only because a held/fresh pin pulled them in —
        # without the age gate
        sys.exit(f"ungated additions: {', '.join(added)}; rerun with them in --fresh, "
                 f"or hold the pulling pin older")
    hashes = collect_hashes(python, pins, tmp)

    lines = [HEADER.format(date=date.today().isoformat())]
    for n, v in sorted(pins.items()):
        hs = sorted(hashes[(n, v)])
        lines.append(f"{n}=={v} \\")
        for j, h in enumerate(hs):
            lines.append(f"    --hash={h}" + (" \\" if j < len(hs) - 1 else ""))

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {out} with {len(pins)} pins")
    for n, v in sorted(pins.items()):
        print(f"  {n}=={v}: {len(hashes[(n, v)])} hash(es)")


def check(python: str, req: Path, lock: Path) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="clipshelf-lockgen-check-"))
    lock_pins = read_pins(lock)
    res = resolve(python, req, tmp, lock_pins)
    current = res.pins

    problems = []
    warnings = []
    for n in sorted(res.yanked):
        v = lock_pins.get(n, current.get(n, "?"))
        problems.append(f"yanked: {n}=={v} (pip installs yanked files under exact pins; "
                        f"pip-audit ignores yank status)")
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

    # end-to-end validation: pip must accept the lock on every shipped platform,
    # not just this host (CI runs on x86_64; the image also ships arm64)
    for plats in PLATFORM_SETS:
        r = subprocess.run(
            [python, "-m", "pip", "install", "--dry-run", "--ignore-installed",
             "--require-hashes", "--no-deps", "-r", str(lock)] + plat_args(plats),
            capture_output=True, text=True)
        if r.returncode != 0:
            print("DRIFT:")
            print(f"  hash validation failed for {plats}")
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
    mk.add_argument("--fresh", nargs="+", metavar="NAME",
                    help="admit the newest release of NAME now, for a security fix")
    ck = sub.add_parser("check")
    ck.add_argument("req", nargs="?", default="requirements.txt")
    ck.add_argument("lock", nargs="?", default="requirements.lock")
    args = ap.parse_args()

    python = sys.executable
    if args.cmd == "make":
        make(python, Path(args.req), Path(args.out), args.fresh)
    else:
        sys.exit(check(python, Path(args.req), Path(args.lock)))


if __name__ == "__main__":
    main()
