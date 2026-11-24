#!/usr/bin/env python3
"""Peak RSS of json.loads on two worst-case import bodies.

One child process per size: the parent builds nothing, so the child's
ru_maxrss is the parse's true peak, reported as the delta over a baseline
child that only starts the interpreter.

Body kinds:
  arrays    [[],[],...] — the element flood: the count of objects is what
            amplifies, and MAX_IMPORT_ELEMENTS now refuses it past 250000
            structural bytes before parsing.
  strings   a few 64 KiB ASCII string literals, each carrying one astral
            character (U+1F600). String contents do not count as elements,
            so this body is admitted under the element budget, and the
            astral character makes CPython store the whole string as four
            bytes per character — about four times the body size.

The 32 MiB probes need roughly 850 MB (arrays) and 350 MB (strings) of
free memory; on a smaller host the OOM killer ends them, which the parent
reports as a negative exit code.

Usage: python3 scripts/bench_import_parse.py [--arrays|--strings] [size_mib ...]
       (default: --arrays 4 8 16 32)
"""
import resource
import subprocess
import sys

CHILD = r'''
import json, resource, sys
mib, kind = int(sys.argv[1]), sys.argv[2]
if kind == "arrays":
    n = mib * 1024 * 1024 // 3
    body = b"[" + b"[]," * (n - 1) + b"[]]"
else:
    unit = b'"' + b"x" * 65536 + "\U0001F600".encode() + b'",'
    n = max(1, mib * 1024 * 1024 // len(unit))
    body = b"[" + unit * (n - 1) + unit[:-1] + b"]"
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
    kind = "arrays"
    sizes = []
    for arg in sys.argv[1:]:
        if arg == "--arrays":
            kind = "arrays"
        elif arg == "--strings":
            kind = "strings"
        else:
            sizes.append(int(arg))
    sizes = sizes or [4, 8, 16, 32]
    baseline = child_maxrss("import resource;"
                            "print(resource.getrusage(resource.RUSAGE_SELF)"
                            ".ru_maxrss)")
    print(f"baseline child rss: {baseline // 1024} MiB ({kind} bodies)")
    for mib in sizes:
        out = subprocess.run([sys.executable, "-c", CHILD, str(mib), kind],
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
