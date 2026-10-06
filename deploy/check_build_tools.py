#!/usr/bin/env python3
"""Fail if the runtime image ships the SQLite build-stage compiler toolchain.

gcc, make and libc6-dev belong only to the discarded sqlite-build stage. This
asks the built image itself (dpkg database and PATH), not the Dockerfile text.
Fails closed: a dpkg-query error counts as a failure, never as "absent".

Usage (docker-publish verify job, before publication):
  docker run --rm --entrypoint python clipshelf:verify deploy/check_build_tools.py
"""
import shutil
import subprocess
import sys

PACKAGES = ("gcc", "make", "libc6-dev", "build-essential")
EXECUTABLES = ("gcc", "cc", "make")


def installed_packages():
    """Return the PACKAGES that dpkg records as installed."""
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package} ${Status}\n", *PACKAGES],
            capture_output=True, text=True,
        )
    except OSError as exc:
        raise SystemExit(f"FAIL: cannot run dpkg-query: {exc}") from exc
    # Exit 1 means a requested name is absent; all other errors fail closed.
    if result.returncode not in (0, 1):
        raise SystemExit(
            f"FAIL: dpkg-query exited {result.returncode}: {result.stderr.strip()}"
        )
    # Removed-but-not-purged packages report "config-files" or "not-installed".
    return sorted(
        line.split()[0] for line in result.stdout.splitlines()
        if line.split() and line.split()[-1] == "installed"
    )


def main():
    packages = installed_packages()
    executables = sorted(
        f"{name}={path}" for name in EXECUTABLES if (path := shutil.which(name))
    )
    if packages or executables:
        print(
            "FAIL: runtime image ships build tools: "
            f"packages={packages} executables={executables}",
            file=sys.stderr,
        )
        return 1
    print(f"PASS: no build tools (packages {PACKAGES}, executables {EXECUTABLES})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
