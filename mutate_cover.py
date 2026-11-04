#!/usr/bin/env python3
"""Per-test-case line coverage of the mutation targets (mutate.py helper).

mutate.py's coverage pass runs each suite command through this wrapper once,
before any mutant exists: sys.monitoring records which test case executed
which line of a target module, and the JSON lands in OUT_JSON:

  {"cases":  {test_id: {target_relpath: [lineno, ...]}},
   "import": {target_relpath: [lineno, ...]}}

"import" holds lines executed outside any unittest.TestCase.run — module
imports at discovery time, setUpClass, module fixtures. Mutants on those
lines run the whole command: coarser, never wrong. A standalone suite script
has no unittest cases, so all its lines land in "import" and mutate.py
treats the whole script as one unit.

How events stay cheap: a meta-path hook fires only for the target modules.
While one executes, global LINE events are on (top-level lines). After it,
every function of that module found via gc gets set_local_events(LINE), so
nothing outside the targets is ever instrumented — the suite runs at close
to native speed and the heavy Django discovery stays far below the timeout.
Limitation: a function created after import (a def nested in another
function) is not instrumented; a mutation site inside one may report
NO_COVERAGE. The current targets define no nested functions.

Usage (mutate.py calls this; do not run it by hand):
  python mutate_cover.py TARGETS_JSON OUT_JSON MODE args...
  MODE django: args are the arguments of `clipshelf.py ...`; the launcher is
      re-created inline (umask, DJANGO_SETTINGS_MODULE, django.setup,
      execute_from_command_line).
  MODE script: args are the arguments of one standalone suite script; it
      runs via runpy with run_name "__main__".

Stdlib only. Nothing is deleted. OUT_JSON is written even when the inner
command fails, so a late failure never loses collected data.
"""
import gc
import json
import os
import runpy
import sys
import tokenize
import types
import unittest


def logical_spans(path):
    """Physical line -> (first, last) of its logical line. CPython fires no
    line events on the continuation lines of a multi-line literal (one
    LOAD_CONST, attributed to the first line), so recorded lines must grow
    back to their full span or mutants on continuation lines report
    NO_COVERAGE despite executed constants."""
    spans = {}
    try:
        with open(path, encoding="utf-8") as fh:
            first = last = None
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type == tokenize.NEWLINE:
                    if first is not None:
                        for row in range(first, last + 1):
                            spans[row] = (first, last)
                    first = last = None
                elif tok.type not in (tokenize.NL, tokenize.COMMENT,
                                      tokenize.INDENT, tokenize.DEDENT,
                                      tokenize.ENDMARKER):
                    if first is None:
                        first = tok.start[0]
                    last = max(last or tok.end[0], tok.end[0])
    except (OSError, UnicodeError, tokenize.TokenError):
        return spans  # no expansion: raw recorded lines only
    return spans


def main(argv):
    targets_file, out_file, mode, inner = argv[0], argv[1], argv[2], argv[3:]
    with open(targets_file, encoding="utf-8") as fh:
        targets = json.load(fh)  # {absolute source path: repo-relative path}
        target_dirs = frozenset(targets)
        target_mods = frozenset(rel[:-3].replace("/", ".") for rel in targets.values())
    spans = {rel: logical_spans(path) for path, rel in targets.items()}

    def expand(rel, lines):
        """Recorded lines grown to their logical-line span (multi-line
        literals record only their first physical line)."""
        table = spans.get(rel)
        if not table:
            return lines
        out = set()
        for row in lines:
            span = table.get(row)
            if span is not None:
                out.update(range(span[0], span[1] + 1))
            else:
                out.add(row)
        return out

    mon = sys.monitoring
    tool = None
    for tid in (5, 4, 3, 2):  # 0 = debuggers and 1 = coverage tools by convention
        try:
            mon.use_tool_id(tid, "mutate-cover")
            tool = tid
            break
        except ValueError:
            continue
    if tool is None:
        sys.exit("mutate_cover: no free sys.monitoring tool id")

    covered = {"__import__": {}}
    phase = ["__import__"]

    def record(code, lineno):
        rel = targets.get(code.co_filename)
        if rel is None:
            rel = targets.get(os.path.realpath(code.co_filename))
        if rel is not None:
            covered[phase[0]].setdefault(rel, set()).add(lineno)

    mon.register_callback(tool, mon.events.LINE, record)

    def instrument(code):
        """Local LINE events for one target code object and its nested ones."""
        mon.set_local_events(tool, code, mon.events.LINE)
        stack = [c for c in code.co_consts if isinstance(c, types.CodeType)]
        while stack:
            inner_code = stack.pop()
            mon.set_local_events(tool, inner_code, mon.events.LINE)
            stack.extend(c for c in inner_code.co_consts
                         if isinstance(c, types.CodeType))

    def instrument_module():
        """Every target function alive now: gc finds decorated and cached
        functions a module-attribute walk would miss."""
        gc.collect()
        for obj in gc.get_objects():
            if (isinstance(obj, (types.FunctionType, types.CoroutineType))
                    and obj.__code__.co_filename in target_dirs):
                instrument(obj.__code__)

    class TargetHook:
        """Meta-path finder for the target modules only: global LINE during
        the module's own execution (top-level lines), local LINE for its
        functions afterwards."""

        def find_spec(self, fullname, path=None, target=None):
            if fullname not in target_mods:
                return None
            for finder in sys.meta_path:
                if finder is self:
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None:
                    original = spec.loader.exec_module

                    def exec_module(module, _original=original):
                        mon.set_events(tool, mon.events.LINE)
                        try:
                            _original(module)
                        finally:
                            mon.set_events(tool, mon.events.NO_EVENTS)
                        instrument_module()

                    spec.loader.exec_module = exec_module  # instance attr shadows class
                    return spec
            return None

    # First in sys.meta_path: the default finders would resolve the target
    # modules before a hook appended last was ever consulted.
    sys.meta_path.insert(0, TargetHook())

    # Bucket switch: unittest.TestCase.run brackets exactly one test case,
    # under Django discovery and plain unittest alike. setUpClass and module
    # imports stay in "__import__".
    original_run = unittest.case.TestCase.run

    def traced_run(self, result=None):
        previous, phase[0] = phase[0], self.id()
        covered.setdefault(phase[0], {})
        try:
            return original_run(self, result)
        finally:
            phase[0] = previous

    unittest.case.TestCase.run = traced_run

    try:
        if mode == "django":
            os.umask(0o077)  # clipshelf.cli.main pins the umask; keep parity
            os.environ.setdefault("DJANGO_SETTINGS_MODULE",
                                  "clipshelf.project.settings")
            import django
            django.setup()
            from django.core.management import execute_from_command_line
            execute_from_command_line(["clipshelf.py", *inner[1:]])
        elif mode == "script":
            sys.argv = list(inner)
            runpy.run_path(inner[0], run_name="__main__")
        else:
            sys.exit(f"mutate_cover: unknown mode {mode!r}")
    finally:
        mon.set_events(tool, mon.events.NO_EVENTS)
        mon.free_tool_id(tool)
        import_lines = covered.pop("__import__")
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump({"cases": {cid: {rel: sorted(expand(rel, lines))
                                       for rel, lines in mods.items()}
                                 for cid, mods in covered.items()},
                       "import": {rel: sorted(expand(rel, lines))
                                  for rel, lines in import_lines.items()}}, fh)


if __name__ == "__main__":
    main(sys.argv[1:])
