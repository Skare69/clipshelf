#!/usr/bin/env python3
"""Mutation testing harness — stdlib only, Python 3.13, zero new dependencies.

Generates single-site mutants (operator swaps, constant tweaks) of the
targeted modules, then runs the repo's own offline test suite once per mutant
inside a workspace holding only the git-tracked files (gitignored personal
data, caches, and credentials never get copied) with a per-mutant throwaway
data dir. Mutant runs execute nothing but the repo's tests: no network, no
other commands. The real checkout is never modified — all writes land in a
copy under the system temp directory's clipshelf-mutation folder. Nothing the
harness creates is ever deleted (operator policy: leftover run dirs are
expected, not leaks).

Each mutant runs only against the tests that can see it. A baseline coverage
pass (mutate_cover.py, sys.monitoring) records which test case executed which
line of the targets; a mutant runs just its covering cases. Lines executed
only at import time run the whole commands whose test modules import the
target (static import map). A line no test executes reports NO_COVERAGE —
it counts like a survivor for the score. A command whose coverage pass
failed falls back to the static map; a map miss falls back to the full
suite: slower, never wrong.


Usage:
  python mutate.py                          # default scope: DEFAULT_TARGETS
  python mutate.py --targets clipshelf/lib.py
  python mutate.py --diff origin/main       # changed in-scope modules only
  python mutate.py --diff v0.9.0            # release review: everything since the tag
  python mutate.py --max-mutants 20         # evenly sampled smoke run
  python mutate.py --fail-under 70          # gate; exit 1 below the score
  python mutate.py self-check               # fast harness self-test, no repo
"""
import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

REPO = Path(__file__).resolve().parent
TEMP_ROOT = Path(tempfile.gettempdir()) / "clipshelf-mutation"

# Scope policy: pure-logic domain modules first. Request handlers, command
# modules and the gate scripts (scripts/, deploy/, check_licenses.py) join the
# scope per change via --diff; wiring (settings, urls, migrations, tests, this
# harness) is never mutated.
DEFAULT_TARGETS = [
    "clipshelf/lib.py",
    "clipshelf/judgment.py",
    "clipshelf/publication.py",
    "clipshelf/asset_files.py",
    "clipshelf/interpretation.py",
]
EXCLUDED_PARTS = ("migrations/", "project/wsgi", "project/apps", "project/signals",
                  "project/settings", "clipshelf/urls")
SCOPE_ROOTS = ("clipshelf/", "scripts/", "deploy/")
GATE_SCRIPTS = ("check_licenses.py",)  # repo root; the launcher and this harness stay out

# Per-mutant test commands: the full CI suite (ci.yml) = Django unittest
# discovery plus the three standalone script suites it runs separately. Tests
# fake all transports, so a mutant run makes no requests.
SUITE_CMDS = [
    [sys.executable, "clipshelf.py", "test", "--verbosity", "0"],
    [sys.executable, "test_clipshelf.py"],
    [sys.executable, "test_sources.py"],
    [sys.executable, "test_properties.py"],
]
# suite baseline ~13s; 120s absorbs pathological mutants without stalling runs
DEFAULT_TIMEOUT = 120
# Coverage wrapper, committed next to this harness; copied into the run dir.
COVER_SCRIPT = "mutate_cover.py"
# Selected invocations run in this order: run_units stops at the first
# failing one, and a Django test subset (~seconds) must not queue behind
# test_sources (~45s in this container).
CHEAP_FIRST = (0, 1, 3, 2)
# Ratchet: 116 evenly spaced sites of the default scope. The old stride
# sampler changed both count and membership when the scope grew: 921 sites /
# stride 8 gave 116; 950 sites / stride 9 gave only 106.
# Floor = the clean score this harness measures: 110/116 = 94.8% on the
# 1030-site scope after the v0.11.0 review's survivors and the 2026-10-15
# rebase drift were pinned by tests (0 timeouts; a non-zero suite exit always
# kills, so noise only raises scores). Re-measured unchanged (110/116 =
# 94.8%, the same six behavioral equivalents) with per-line test selection on
# 2026-11-04: import-time-line mutants run whole suite commands, so on a
# loaded machine one command can cross DEFAULT_TIMEOUT and read as a kill.
# The six remaining survivors are
# behaviorally equivalent: the "" hostname fallback at clipshelf/lib.py:89,
# the screened-init flag (its value only matters past positive steer
# thresholds), the PIL draft hint mode, the invisible-range end, the severity
# fallback (1.0 stays under the 1.5 withhold bound), and a warning cap no
# message can exceed. Re-measure whenever the scope changes: membership drift
# is large (956 -> 971 sites moved the score from 66.4% to 79.3%), and
# defect-inflated earlier readings (stale bytecode, 76.7%) are not baselines.
# ponytail: margin is rounding only; pin the remaining survivors to raise
# the floor further.
DEFAULT_FAIL_UNDER = 94

CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
       ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
       ast.Is: ast.IsNot, ast.IsNot: ast.Is}
BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
       ast.FloorDiv: ast.Mod, ast.Mod: ast.FloorDiv}


def _docstring_ids(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                ids.add(id(first.value))
    return ids


def collect_sites(tree):
    """Return mutable sites as (walk_index, label, apply) for one tree."""
    docs = _docstring_ids(tree)
    nodes = list(ast.walk(tree))
    sites = []
    for i, n in enumerate(nodes):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in CMP:
            old, new = type(n.ops[0]), CMP[type(n.ops[0])]
            sites.append((i, f"{old.__name__}->{new.__name__}",
                          lambda node, new=new: setattr(node, "ops", [new()])))
        elif isinstance(n, ast.BoolOp) and type(n.op) in (ast.And, ast.Or):
            new = ast.Or() if isinstance(n.op, ast.And) else ast.And()
            sites.append((i, f"{type(n.op).__name__}->{type(new).__name__}",
                          lambda node, new=new: setattr(node, "op", new)))
        elif isinstance(n, ast.BinOp) and type(n.op) in BIN:
            old, new = type(n.op), BIN[type(n.op)]
            sites.append((i, f"{old.__name__}->{new.__name__}",
                          lambda node, new=new: setattr(node, "op", new())))
        elif isinstance(n, ast.AugAssign) and type(n.op) in BIN:
            old, new = type(n.op), BIN[type(n.op)]
            sites.append((i, f"{old.__name__}={new.__name__}=",
                          lambda node, new=new: setattr(node, "op", new())))
        elif isinstance(n, ast.Constant):
            v = n.value
            if v is True or v is False:
                sites.append((i, f"{v}->{not v}", lambda node: setattr(node, "value", not node.value)))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                sites.append((i, f"{v}->{v + 1}", lambda node: setattr(node, "value", node.value + 1)))
            elif isinstance(v, str) and id(n) not in docs and len(v) < 200:
                sites.append((i, 'str->+"x"', lambda node: setattr(node, "value", node.value + "x")))
    return nodes, sites


def mutant_sources(tree, sites):
    """Yield (site, mutated_source) — one mutation per generated source."""
    for index, label, apply in sites:
        copy = deepcopy(tree)
        apply(list(ast.walk(copy))[index])
        yield (index, label), ast.unparse(copy)


def run_suite(ws, env, cmd, timeout):
    """Run the test command; None return means timeout (the only process the
    harness ever terminates is this child, which would otherwise hang on an
    infinite-loop mutant)."""
    try:
        proc = subprocess.run(cmd, cwd=ws, env=env, capture_output=True,
                              text=True, errors="replace", timeout=timeout)
        return proc.returncode, f"{proc.stdout}\n{proc.stderr}"[-4000:]
    except subprocess.TimeoutExpired as exc:
        out, err = exc.stdout or "", exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return None, f"{out}\n{err}"[-4000:]


def run_suites(ws, env, timeout):
    """Run every CI suite command; stop at the first failing one."""
    for cmd in SUITE_CMDS:
        code, out = run_suite(ws, env, cmd, timeout)
        if code != 0:
            return code, out
    return 0, ""


def django_cmd(cmd):
    """True for the suite command that runs Django's own test discovery."""
    return cmd[1] == "clipshelf.py" and "test" in cmd[2:]


def write_wrapper(ws):
    """Copy the coverage wrapper into the run dir's workspace."""
    shutil.copy2(REPO / COVER_SCRIPT, ws / COVER_SCRIPT)


def test_module_paths(ws, cmd):
    """Files whose imports feed the static target map for one command."""
    if not django_cmd(cmd):
        return [ws / cmd[1]]
    out = []
    for path in sorted(ws.rglob("test*.py")):
        parts = path.relative_to(ws).parts
        if len(parts) == 1 or (ws / parts[0] / "__init__.py").is_file():
            out.append(path)  # discovery imports top-level files and package members
    return out


def imported_modules(path):
    """Dotted names one module imports; unparsable files contribute nothing."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


def importing_cmds(ws, targets, cmds):
    """Static per-target map (selection part 1): the commands whose test
    modules import a target module, directly or through any import chain in
    the workspace (a Django test reaching clipshelf.lib through worker must
    still map, or fallback runs lose its kills). A map miss later falls
    back to the full suite: slower, never wrong."""
    want = {t[:-3].replace("/", ".") for t in targets}
    imports = {}
    for path in ws.rglob("*.py"):
        rel = path.relative_to(ws).as_posix()
        imports[rel[:-3].replace("/", ".")] = imported_modules(path)
    out = set()
    for i, cmd in enumerate(cmds):
        for path in test_module_paths(ws, cmd):
            name = path.relative_to(ws).as_posix()[:-3].replace("/", ".")
            seen, frontier, reach = {name}, [name], False
            while frontier and not reach:
                for nxt in imports.get(frontier.pop(), ()):
                    if nxt in want:
                        reach = True
                        break
                    if nxt not in seen and nxt in imports:
                        seen.add(nxt)
                        frontier.append(nxt)
            if reach:
                out.add(i)
                break
    return out


def fold_script_cases(data):
    """A script suite command is one unit: fold its per-case lines into the
    import bucket so any covered line runs the whole script."""
    for mods in data["cases"].values():
        for rel, lines in mods.items():
            data["import"].setdefault(rel, set()).update(lines)
    data["cases"] = {}
    return data


def coverage_pass(ws, targets, temp_root, timeout, log):
    """One wrapper run per suite command (selection part 2): which test case
    executed which line of the targets. Returns a list parallel to
    SUITE_CMDS; a command whose pass failed maps to None and its mutants
    later fall back to the static map.

    sys.monitoring multiplies suite wall time by roughly 1.5-2x even with
    local events; the pass runs once per command, so it gets 4x the
    per-mutant budget. Timing-sensitive worker/DB tests flaked once under
    instrumentation, so a failed pass retries twice with fresh data dirs
    before falling back to the static map."""
    write_wrapper(ws)
    cov_timeout = timeout * 4
    cover = []
    for i, cmd in enumerate(SUITE_CMDS):
        targets_json = temp_root / f"cov-targets-{i}.json"
        targets_json.write_text(
            json.dumps({str(ws / rel): rel for rel in targets}), encoding="utf-8")
        out_json = temp_root / f"cov-{i}.json"
        mode = "django" if django_cmd(cmd) else "script"
        wrapped = [sys.executable, COVER_SCRIPT, str(targets_json), str(out_json),
                   mode, *cmd[1:]]
        data, code, out = None, None, ""
        for attempt in (1, 2, 3):
            env = dict(os.environ)
            env["CLIPSHELF_DATA_DIR"] = str(temp_root / f"data-cov-{i}-{attempt}")
            env["CLIPSHELF_DEBUG"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"  # see score_run: stale pyc skews scores
            start = time.monotonic()
            code, out = run_suite(ws, env, wrapped, cov_timeout)
            dt = time.monotonic() - start
            if code == 0 and out_json.exists():
                raw = json.loads(out_json.read_text(encoding="utf-8"))
                parsed = {"cases": {cid: {rel: set(lines) for rel, lines in mods.items()}
                                    for cid, mods in raw["cases"].items()},
                          "import": {rel: set(lines)
                                     for rel, lines in raw["import"].items()}}
                data = fold_script_cases(parsed) if mode == "script" else parsed
                log(f"coverage: {cmd[1]} {len(data['cases'])} case(s), "
                    f"{len(data['import'])} module(s) with import-time lines "
                    f"{dt:.1f}s")
                break
            log(f"coverage pass failed for {cmd[1]} (exit {code}), "
                f"attempt {attempt}/3 {dt:.1f}s")
        if data is None:
            log(f"no coverage for {cmd[1]}; static map fallback\n{out}"[:2000])
        cover.append(data)
    return cover


def unit_plan(rel, lineno, cover, static):
    """Invocations for one mutant site: (command index, case ids); None ids
    mean the whole command. Returns ([], True) for a line no test executes:
    NO_COVERAGE. Units come back cheapest first: run_units stops at the
    first failing invocation, so a fast kill must not queue behind a slow
    command (test_sources alone is ~45s here)."""
    units = []
    for i, data in enumerate(cover):
        if data is None:
            if i in static:  # static per-target map (selection part 1)
                units.append((i, None))
            continue
        if lineno in data["import"].get(rel, ()):  # import-time line: whole command
            units.append((i, None))
            continue
        ids = sorted(cid for cid, mods in data["cases"].items()
                     if lineno in mods.get(rel, ()))
        if ids:
            units.append((i, ids))
    if units:
        units.sort(key=lambda u: CHEAP_FIRST.index(u[0]))
        return units, False
    if any(data is None for data in cover):
        # static map miss: full suite — slower, never wrong
        return [(i, None) for i in CHEAP_FIRST], False
    return [], True


def case_cmd(i, ids):
    """One invocation for unit (i, ids): covering labels replace the full run."""
    cmd = SUITE_CMDS[i]
    if ids is None:
        return list(cmd)
    if django_cmd(cmd):
        return [sys.executable, cmd[1], "test", *ids, *cmd[3:]]
    raise AssertionError(f"case ids for a script suite command: {cmd}")


def run_units(ws, env, units, timeout):
    """Run one mutant's planned invocations; stop at the first failing one."""
    for _i, ids in units:
        code, out = run_suite(ws, env, case_cmd(_i, ids), timeout)
        if code != 0:
            return code, out
    return 0, ""


def sample(items, n):
    """Exactly n evenly spaced items (first included). The old stride slice
    returned ceil(len/stride) items: 950 sites at --max-mutants 116 gave 106."""
    return [items[i * len(items) // n] for i in range(n)] if len(items) > n else list(items)


def score_run(target_files, ws, temp_root, timeout, max_mutants, log,
              cover=None, static=frozenset()):
    """Copy sources in, mutate one site at a time, select the covering
    invocations, classify, restore. cover is coverage_pass output (or None:
    every mutant runs the full suite)."""
    if cover is None:
        cover = [None] * len(SUITE_CMDS)
    total = killed = 0
    survived = []
    nocovs = []
    baseline_text = {}
    for rel in target_files:
        if not (ws / rel).resolve().is_relative_to(ws.resolve()):
            raise ValueError(f"mutation target escapes the workspace: {rel}")
        baseline_text[rel] = (ws / rel).read_text(encoding="utf-8")
    mutants = []
    for rel in target_files:
        tree = ast.parse(baseline_text[rel])
        nodes, sites = collect_sites(tree)
        for (index, label), source in mutant_sources(tree, sites):
            mutants.append((rel, nodes[index].lineno, label, source))
    if max_mutants and len(mutants) > max_mutants:
        log(f"sampling {max_mutants} of {len(mutants)} sites, evenly spaced")
        mutants = sample(mutants, max_mutants)
    log(f"{len(mutants)} mutants across {len(target_files)} module(s)")
    for n, (rel, lineno, label, source) in enumerate(mutants, 1):
        (ws / rel).write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env["CLIPSHELF_DATA_DIR"] = str(temp_root / f"data-{n}")
        env["CLIPSHELF_DEBUG"] = "1"
        # CPython reuses a __pycache__ entry when source mtime (whole seconds)
        # and size match, so without this a fast-crashing mutant can leave its
        # bytecode to the next same-size mutant of the same file.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        units, nocov = unit_plan(rel, lineno, cover, static)
        start = time.monotonic()
        code, out = (0, "") if nocov else run_units(ws, env, units, timeout)
        dt = time.monotonic() - start
        (ws / rel).write_text(baseline_text[rel], encoding="utf-8")  # restore
        total += 1
        if nocov:
            status = "NO_COVERAGE"  # counts like a survivor for the score
            nocovs.append((rel, lineno, label, baseline_text[rel]))
        else:
            status = "SURVIVED" if code == 0 else ("TIMEOUT" if code is None else "KILLED")
            if code == 0:
                survived.append((rel, lineno, label, baseline_text[rel]))
            else:
                if out:
                    log(f"  mutant diagnostics:\n{out}")
                killed += 1
        # The Ottr board parses this line from a stdout tail: keep its format.
        log(f"[{n}/{len(mutants)}] {rel}:{lineno} {label} {status} {dt:.1f}s"
            f" ({len(survived)} survived)")
    return total, killed, survived, nocovs


def report_survivors(survived, log):
    for rel, lineno, label, source in survived:
        lines = source.splitlines()
        line = lines[lineno - 1].strip() if lineno <= len(lines) else ""
        log(f"survived: {rel}:{lineno} {label}  |  {line[:120]}")


def report_nocov(nocovs, log):
    """Sites no test executes: pin them with a test or accept the survivor."""
    for rel, lineno, label, source in nocovs:
        lines = source.splitlines()
        line = lines[lineno - 1].strip() if lineno <= len(lines) else ""
        log(f"no coverage: {rel}:{lineno} {label}  |  {line[:120]}")


def changed_targets(base_ref):
    """In-scope modules changed vs base_ref. Deleted files are dropped (--diff-filter=d)
    so a branch that removes a module cannot fail the run; tests and wiring are
    never mutated."""
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=d",
                          f"{base_ref}...HEAD"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    targets = []
    for name in out.stdout.splitlines():
        if (name.endswith(".py")
                and (name.startswith(SCOPE_ROOTS) or name in GATE_SCRIPTS)
                and not Path(name).name.startswith("test")
                and not any(part in name for part in EXCLUDED_PARTS)):
            targets.append(name)
    return targets


def copy_tracked(repo, ws):
    """Copy the working-tree content of git-tracked files only; ignored and
    untracked files (personal data, caches, keystores) stay behind. Symlinks
    are skipped so a tracked link cannot pull in a file outside the repo."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo,
                         capture_output=True, check=True)
    copied = set()
    for rel in out.stdout.decode("utf-8").split("\0"):
        src = repo / rel
        if rel and src.is_file() and not src.is_symlink():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, ws / rel)
            copied.add(rel)
    return copied


def run(args, log=print):
    start = time.monotonic()
    if args.diff:
        targets = changed_targets(args.diff)
        if not targets:
            log(f"no in-scope changed modules vs {args.diff}")
            return 0
    else:
        targets = args.targets or DEFAULT_TARGETS
    rels = []
    for t in targets:  # absolute or ../ paths would write outside the copied workspace
        path = (REPO / t).resolve()
        if not path.is_relative_to(REPO):
            log(f"target outside the repo: {t}")
            return 2
        rels.append(path.relative_to(REPO).as_posix())
    targets = rels
    missing = [t for t in targets if not (REPO / t).exists()]
    if missing:
        log(f"target(s) not found: {', '.join(missing)}")
        return 2
    run_id = time.strftime("%Y%m%d-%H%M%S")
    temp_root = TEMP_ROOT / f"run-{run_id}"
    ws = temp_root / "workspace"
    log(f"workspace: {ws} (kept after the run; nothing is deleted)")
    copied = copy_tracked(REPO, ws)
    untracked = [t for t in targets if Path(t).as_posix() not in copied]
    if untracked:
        log(f"target(s) not tracked by git: {', '.join(untracked)}")
        return 2
    env = dict(os.environ)
    env["CLIPSHELF_DATA_DIR"] = str(temp_root / "data-baseline")
    env["CLIPSHELF_DEBUG"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # see score_run: stale pyc skews scores
    log(f"baseline: {SUITE_CMDS}")
    code, out = run_suites(ws, env, args.timeout)
    if code != 0:
        log(f"baseline suite fails (exit {code}); mutation scores are meaningless:\n{out}")
        return 2
    cover = coverage_pass(ws, targets, temp_root, args.timeout, log)
    missing = [i for i, data in enumerate(cover) if data is None]
    if missing:
        log(f"no coverage data for command(s) {missing}; static map fallback")
    static = importing_cmds(ws, targets, SUITE_CMDS)
    total, killed, survived, nocovs = score_run(targets, ws, temp_root,
                                                args.timeout, args.max_mutants,
                                                log, cover=cover, static=static)
    score = 100.0 * killed / total if total else 0.0
    log(f"\nscore: {killed}/{total} killed = {score:.1f}% (gate: >= {args.fail_under}%)")
    report_survivors(survived, log)
    report_nocov(nocovs, log)
    log(f"kept (not deleted): {temp_root}")
    log(f"wall time: {time.monotonic() - start:.1f}s")
    return 0 if score >= args.fail_under else 1


def self_check():
    """Assert the mutator produces the expected classes, that the runner
    kills a caught mutant and reports an uncaught one, and that per-mutant
    selection runs only the covering invocations. Hermetic: its own
    temp workspace, plain unittest, no repo code."""
    global run_suite, REPO, TEMP_ROOT
    snippet = "def f(a, b):\n    if a == b:\n        return a + b\n    return a * b\n"
    labels = [label for _, label, _ in collect_sites(ast.parse(snippet))[1]]
    for expect in ("Eq->NotEq", "Add->Sub", "Mult->Div"):
        assert expect in labels, (expect, labels)
    assert not any("str->" in label for label in labels), labels  # no docstring noise
    picked = sample(list(range(950)), 116)
    assert len(picked) == 116 and picked[:2] == [0, 8] and picked[-1] == 941, picked

    report_source = "def sample():\n    return 1  # original formatting\n"

    ws = TEMP_ROOT / f"selfcheck-{time.strftime('%Y%m%d-%H%M%S')}" / "ws"
    ws.mkdir(parents=True)
    (ws / "report.py").write_text(report_source, encoding="utf-8")
    real_run_suite = run_suite
    try:
        run_suite = lambda *args: (0, "")
        _, _, survived, _ = score_run(["report.py"], ws, ws, 60, 1, lambda _msg: None)
    finally:
        run_suite = real_run_suite
    report_lines = []
    report_survivors(survived, report_lines.append)
    assert "return 1  # original formatting" in report_lines[0]
    assert "original formatting" not in ast.unparse(ast.parse(report_source))
    (ws / "module.py").write_text(
        "def limit(x):\n    return 100 if x > 100 else x\n", encoding="utf-8")
    (ws / "test_module.py").write_text(
        "import unittest, module\n\n\nclass T(unittest.TestCase):\n"
        "    def test_lower(self):\n        self.assertEqual(module.limit(50), 50)\n",
        encoding="utf-8")
    tree = ast.parse((ws / "module.py").read_text(encoding="utf-8"))
    sites = collect_sites(tree)[1]
    assert len(sites) == 3, sites  # two int constants, one compare
    results = []
    for (index, label), source in mutant_sources(tree, sites):
        (ws / "module.py").write_text(source, encoding="utf-8")
        code, _ = run_suite(ws, dict(os.environ), [sys.executable, "-m", "unittest", "test_module"], 60)
        (ws / "module.py").write_text(ast.unparse(tree), encoding="utf-8")
        results.append((label, "KILLED" if code else "SURVIVED"))
    # the untested upper branch lets both constant mutants through; the
    # flipped comparison is caught by the one test
    assert sorted(results) == sorted([("100->101", "SURVIVED"), ("100->101", "SURVIVED"),
                                      ("Gt->LtE", "KILLED")]), results

    # Progress line: the Ottr board parses it from a stdout tail, so pin the
    # exact shape and the running survivor count (KILLED and TIMEOUT do not
    # count; the count includes the current mutant).
    progress = []
    calls = []

    def fake_run_suite(_ws, _env, _cmd, _timeout):
        calls.append(_cmd)
        return (1, "") if len(calls) == 1 else (0, "")  # first mutant killed

    try:
        run_suite = fake_run_suite
        score_run(["module.py"], ws, ws, 60, 0, progress.append)
    finally:
        run_suite = real_run_suite
    progress = [line for line in progress if line.startswith("[")]
    assert len(progress) == 3, progress
    for count, line in zip((0, 1, 2), progress):
        head, sep, tail = line.rpartition(" (")
        assert sep and tail == f"{count} survived)", (count, line)
        fields = head.split()
        assert len(fields) == 5, line
        assert fields[0].startswith("[") and fields[0].endswith("/3]"), line
        assert fields[1].startswith("module.py:"), line
        assert fields[3] in ("KILLED", "SURVIVED", "TIMEOUT", "NO_COVERAGE"), line
        assert fields[4].endswith("s"), line
    code, out = run_suite(
        ws, dict(os.environ),
        [sys.executable, "-c",
         "import sys; print('stderr diagnostic', file=sys.stderr); sys.exit(1)"], 60)
    assert code == 1 and "stderr diagnostic" in out, out

    # per-mutant selection: a line's covering cases only; import-time lines
    # run the whole command; an unreached line is NO_COVERAGE and counts
    # like a survivor; a static-map hit runs one command; a map miss runs
    # the full SUITE_CMDS.
    sel = ws.parent / "sel" / "ws"
    sel.mkdir(parents=True)
    (sel / "mod2.py").write_text(
        "LIMIT = 100\n\n\ndef f(x):\n    if x > 10:\n"
        "        return x * 2\n    return x + 1\nif LIMIT > 200:\n    DEAD = 0\n",
        encoding="utf-8")
    (sel / "test_mod2.py").write_text(
        "import unittest, mod2\n\n\nclass T(unittest.TestCase):\n"
        "    def test_small(self):\n        self.assertEqual(mod2.f(3), 4)\n\n"
        "    def test_big(self):\n        self.assertEqual(mod2.f(20), 40)\n",
        encoding="utf-8")
    django_cover = {"cases": {"test_mod2.T.test_small": {"mod2.py": {5, 7}},
                              "test_mod2.T.test_big": {"mod2.py": {5, 6}}},
                    "import": {"mod2.py": {1, 4, 8}}}
    script_cover = {"cases": {}, "import": {"mod2.py": {1, 4, 8}}}  # imports mod2, never calls f
    cover = [django_cover, script_cover, script_cover, script_cover]
    calls = []
    sel_log = []

    def fake(_ws, _env, _cmd, _timeout):
        calls.append(_cmd)
        return (0, "")

    try:
        run_suite = fake
        total, killed, survived, nocovs = score_run(["mod2.py"], sel, sel, 60, 0,
                                                    sel_log.append, cover=cover)
    finally:
        run_suite = real_run_suite
    assert (total, killed, len(survived), len(nocovs)) == (10, 0, 9, 1), \
        (total, killed, survived, nocovs)
    assert nocovs[0][1] == 9 and nocovs[0][2] == "0->1", nocovs  # DEAD = 0: unreached
    django_full = [sys.executable, "clipshelf.py", "test", "--verbosity", "0"]
    both = [sys.executable, "clipshelf.py", "test", "test_mod2.T.test_big",
            "test_mod2.T.test_small", "--verbosity", "0"]
    big = [sys.executable, "clipshelf.py", "test", "test_mod2.T.test_big",
           "--verbosity", "0"]
    small = [sys.executable, "clipshelf.py", "test", "test_mod2.T.test_small",
             "--verbosity", "0"]
    assert sorted(map(str, calls)) == sorted(map(str, [
        django_full, django_full, django_full, both, both, big, big,
        small, small, SUITE_CMDS[1], SUITE_CMDS[1], SUITE_CMDS[1],
        SUITE_CMDS[2], SUITE_CMDS[2], SUITE_CMDS[2],
        SUITE_CMDS[3], SUITE_CMDS[3], SUITE_CMDS[3]])), calls
    statuses = [line.split()[3] for line in sel_log if line.startswith("[")]
    assert statuses.count("SURVIVED") == 9 and statuses.count("NO_COVERAGE") == 1, \
        statuses
    assert sum(line.endswith("(9 survived)") for line in sel_log) == 1, sel_log

    calls.clear()
    try:
        run_suite = fake
        result = score_run(["mod2.py"], sel, sel, 60, 0, lambda _m: None,
                           cover=[None] * len(SUITE_CMDS), static={0})
    finally:
        run_suite = real_run_suite
    assert result == (10, 0, result[2], []), result  # no NO_COVERAGE: map hit
    assert len(calls) == 10 and all(cmd == SUITE_CMDS[0] for cmd in calls), calls[:2]

    calls.clear()
    try:
        run_suite = fake
        result = score_run(["mod2.py"], sel, sel, 60, 0, lambda _m: None)
    finally:
        run_suite = real_run_suite
    assert result == (10, 0, result[2], []), result
    # map miss: full suite, cheapest command first
    assert calls[:4] == [list(SUITE_CMDS[i]) for i in CHEAP_FIRST], calls[:4]
    assert len(calls) == 10 * len(SUITE_CMDS), len(calls)

    # wrapper script mode end to end: a real subprocess over plain unittest;
    # case ids keep the __main__ prefix, fold_script_cases makes the whole
    # script one unit.
    wrap = ws.parent / "wrap" / "ws"
    wrap.mkdir(parents=True)
    (wrap / "mod3.py").write_text("def g(x):\n    return x * 3\n", encoding="utf-8")
    (wrap / "test_mod3.py").write_text(
        "import unittest, mod3\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(mod3.g(1), 3)\n\n"
        "    def test_b(self):\n        self.assertEqual(mod3.g(0), 0)\n\n\n"
        "if __name__ == \"__main__\":\n    unittest.main()\n",
        encoding="utf-8")
    write_wrapper(wrap)
    targets_json = wrap / "targets.json"
    targets_json.write_text(json.dumps({str(wrap / "mod3.py"): "mod3.py"}),
                            encoding="utf-8")
    out_json = wrap / "cover.json"
    wrap_env = dict(os.environ, CLIPSHELF_DATA_DIR=str(wrap / "data"),
                    CLIPSHELF_DEBUG="1", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "mutate_cover.py", str(targets_json), str(out_json),
         "script", "test_mod3.py"],
        cwd=wrap, env=wrap_env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    raw = json.loads(out_json.read_text(encoding="utf-8"))
    assert set(raw["cases"]) == {"__main__.T.test_a", "__main__.T.test_b"}, raw
    assert raw["cases"]["__main__.T.test_a"]["mod3.py"] == [2], raw
    assert raw["cases"]["__main__.T.test_b"]["mod3.py"] == [2], raw
    assert raw["import"]["mod3.py"] == [1], raw
    folded = fold_script_cases({"cases": {"__main__.T.test_a": {"mod3.py": {2}}},
                                "import": {"mod3.py": {1, 2}}})
    assert folded == {"cases": {}, "import": {"mod3.py": {1, 2}}}, folded

    # target confinement: an absolute in-repo target mutates only the copy,
    # and a ../ target is refused before anything is copied or written. The
    # same fake repo pins --diff: a deleted module is skipped, the wiring
    # exclusions hold, and neither the baseline nor a mutant env may let
    # CPython write bytecode (a stale __pycache__ entry skews scores).
    fake_repo = (ws.parent / "repo").resolve()
    files = {
        "mod.py": "def f(a):\n    return a + 1\n",
        "clipshelf/lib.py": "LIMIT = 9\n",
        "clipshelf/gone.py": "GONE = 1\n",
        "clipshelf/urls.py": "urlpatterns = []\n",
        "clipshelf/project/settings.py": "DEBUG = True\n",
        "clipshelf/migrations/0001_init.py": "operations = []\n",
        "scripts/lock_deps.py": "PINNED = {}\n",
        "deploy/check_build_tools.py": "TOOLS = []\n",
        "check_licenses.py": "LICENSES = ()\n",
        "test_thing.py": "X = 1\n",
        "mutate.py": "HARNESS = 1\n",
        "mutate_cover.py": "COVER = 1\n",
        "clipshelf.py": "main()\n",
    }
    git_id = ["-c", "user.email=h@c", "-c", "user.name=h"]
    for rel, text in files.items():
        path = fake_repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"],
                git_id + ["commit", "-qm", "base"], ["tag", "base"]):
        subprocess.run(["git", *cmd], cwd=fake_repo, check=True, capture_output=True)
    for rel, text in files.items():  # every module changes; one module is deleted
        (fake_repo / rel).write_text(text + "# changed\n", encoding="utf-8")
    (fake_repo / "clipshelf" / "gone.py").unlink()
    subprocess.run(["git", *git_id, "commit", "-aqm", "head"], cwd=fake_repo,
                   check=True, capture_output=True)
    outside = ws.parent / "x.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    source = fake_repo / "mod.py"
    original = source.read_bytes(), outside.read_bytes()
    seen, envs = [], []
    real = run_suite, REPO, TEMP_ROOT
    try:
        run_suite = lambda _ws, env, _cmd, _timeout: (
            seen.append(source.read_bytes()), envs.append(dict(env)), (0, ""))[2]
        REPO, TEMP_ROOT = fake_repo, ws.parent / "runs"
        scope = changed_targets("base")
        assert set(scope) == {"clipshelf/lib.py", "scripts/lock_deps.py",
                              "deploy/check_build_tools.py",
                              "check_licenses.py"}, scope
        args = argparse.Namespace(diff="base", targets=None, timeout=60,
                                  max_mutants=0, fail_under=0)
        logs = []
        assert run(args, logs.append) == 0, logs  # deleted module must not exit 2
        assert not any("not found" in line for line in logs), logs
        args = argparse.Namespace(diff=None, targets=[str(source)], timeout=60,
                                  max_mutants=0, fail_under=0)
        logs = []
        assert run(args, logs.append) == 0, logs
        assert len(seen) > 1 and set(seen) == {original[0]}, seen
        args.targets = ["../x.py"]
        logs = []
        assert run(args, logs.append) != 0, logs
        assert any("outside the repo" in line for line in logs), logs
    finally:
        run_suite, REPO, TEMP_ROOT = real
    assert (source.read_bytes(), outside.read_bytes()) == original
    baseline = [e for e in envs if e["CLIPSHELF_DATA_DIR"].endswith("data-baseline")]
    assert baseline and len(envs) > len(baseline), (len(baseline), len(envs))
    assert all(e.get("PYTHONDONTWRITEBYTECODE") == "1" for e in envs), envs
    print(f"self-check ok ({ws})")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # each line reaches a redirected log at once
    if sys.argv[1:2] == ["self-check"]:
        self_check()
        return
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--targets", nargs="+", metavar="FILE", help="modules to mutate")
    parser.add_argument("--diff", metavar="REF", help="mutate only in-scope modules changed vs REF")
    parser.add_argument("--max-mutants", type=int, default=0, help="evenly sample this many sites")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per mutant run")
    parser.add_argument("--fail-under", type=float, default=DEFAULT_FAIL_UNDER,
                        help="exit 1 below this mutation score percent")
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
