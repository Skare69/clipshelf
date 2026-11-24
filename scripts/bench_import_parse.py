#!/usr/bin/env python3
"""Peak RSS of json.loads on a worst-case import body ([[],[],...]).

One child process per size: the parent builds nothing, so the child's
ru_maxrss is the parse's true peak, reported as the delta over a baseline
child that only starts the interpreter. The 32 MiB probe needs about
750 MB of free memory (about 22 MiB of RSS per MiB of body, measured); on
a smaller host the OOM killer ends it, which the parent reports as a
negative exit code. Admission refuses such a body before parsing since the
MAX_IMPORT_ELEMENTS element budget (clipshelf/imports.py); this script
measures the raw cost that budget exists to bound.

Usage: python3 scripts/bench_import_parse.py [size_mib ...]  (default 4 8 16 32)
"""
import resource
import subprocess
import sys

CHILD = r'''
import json, resource, sys
mib = int(sys.argv[1])
n = mib * 1024 * 1024 // 3
body = b"[" + b"[]," * (n - 1) + b"[]]"
items = len(json.loads(body))
print(items, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
'''


def child_maxrss(code):
    """Run one child, return its ru_maxrss in KiB (None when it died)."""
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return int(out.stdout.strip())


def main():
    sizes = [int(arg) for arg in sys.argv[1:]] or [4, 8, 16, 32]
    baseline = child_maxrss("import resource;"
                            "print(resource.getrusage(resource.RUSAGE_SELF)"
                            ".ru_maxrss)")
    print(f"baseline child rss: {baseline // 1024} MiB")
    for mib in sizes:
        out = subprocess.run([sys.executable, "-c", CHILD, str(mib)],
                             capture_output=True, text=True)
        if out.returncode != 0:
            why = ("killed; host memory below the "
                   f"~{mib * 22} MiB this parse needs"
                   if out.returncode < 0 else out.stderr.strip().splitlines()[-1:])
            print(f"{mib:>3}MiB  exit={out.returncode} ({why})")
            continue
        items, rss = out.stdout.split()
        delta = (int(rss) - baseline) // 1024
        print(f"{mib:>3}MiB  items={int(items):,}  peak_rss_delta={delta}MiB"
              f"  ({delta / mib:.1f} MiB per MiB of body)")


if __name__ == "__main__":
    main()
