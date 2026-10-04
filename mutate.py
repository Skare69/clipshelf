#!/usr/bin/env python3
"""Mutation testing harness — stdlib only, Python 3.13, zero new dependencies.

Generates single-site mutants (operator swaps, constant tweaks) of the
targeted modules, then runs the repo's own offline test suite once per mutant
inside a copied workspace with a per-mutant throwaway data dir. Mutant runs
execute nothing but the repo's tests: no network, no other commands. The real
checkout is never modified — all writes land in a copy under
the system temp directory's clipshelf-mutation folder. Nothing the harness creates
is ever deleted (operator policy: leftover run dirs are expected, not leaks).

Usage:
  python mutate.py                          # default scope: DEFAULT_TARGETS
  python mutate.py --targets clipshelf/lib.py
  python mutate.py --diff origin/main       # changed in-scope modules only
  python mutate.py --max-mutants 20         # evenly sampled smoke run
  python mutate.py --fail-under 70          # gate; exit 1 below the score
  python mutate.py self-check               # fast harness self-test, no repo
"""
import argparse
import ast
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

# Scope policy: pure-logic domain modules first. Request handlers and command
# modules join the scope per change via --diff; wiring (settings, urls,
# migrations, tests, this harness) is never mutated.
DEFAULT_TARGETS = [
    "clipshelf/lib.py",
    "clipshelf/judgment.py",
    "clipshelf/publication.py",
    "clipshelf/asset_files.py",
    "clipshelf/interpretation.py",
]
EXCLUDED_PARTS = ("migrations/", "project/wsgi", "project/apps", "project/signals")

# Per-mutant test command: unittest discovery via the Django wrapper, the same
# suite CI runs. Tests fake all transports, so a mutant run makes no requests.
TEST_CMD = [sys.executable, "clipshelf.py", "test", "--verbosity", "0"]
# suite baseline ~10s; 120s absorbs pathological mutants without stalling runs
DEFAULT_TIMEOUT = 120
# Ratchet floor from the first measured run: 120-mutant stratified sample of
# the default scope scored ~59% killed (bound 58.3-60.0%); gate sits ~1
# sampling-margin (±4.5pts at n=120) below it. Raise as tests improve; the
# survived-mutant log names where. Never lower.
DEFAULT_FAIL_UNDER = 55

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


def score_run(target_files, ws, temp_root, timeout, max_mutants, log):
    """Copy sources in, mutate one site at a time, classify, restore."""
    total = killed = 0
    survived = []
    baseline_text = {}
    for rel in target_files:
        baseline_text[rel] = (ws / rel).read_text(encoding="utf-8")
    mutants = []
    for rel in target_files:
        tree = ast.parse(baseline_text[rel])
        nodes, sites = collect_sites(tree)
        for (index, label), source in mutant_sources(tree, sites):
            mutants.append((rel, nodes[index].lineno, label, source))
    if max_mutants and len(mutants) > max_mutants:
        stride = -(-len(mutants) // max_mutants)
        log(f"sampling {max_mutants} of {len(mutants)} sites, stride {stride}")
        mutants = mutants[::stride]
    log(f"{len(mutants)} mutants across {len(target_files)} module(s)")
    for n, (rel, lineno, label, source) in enumerate(mutants, 1):
        (ws / rel).write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env["CLIPSHELF_DATA_DIR"] = str(temp_root / f"data-{n}")
        env["CLIPSHELF_DEBUG"] = "1"
        start = time.monotonic()
        code, out = run_suite(ws, env, TEST_CMD, timeout)
        dt = time.monotonic() - start
        (ws / rel).write_text(baseline_text[rel], encoding="utf-8")  # restore
        total += 1
        status = "SURVIVED" if code == 0 else ("TIMEOUT" if code is None else "KILLED")
        if code == 0:
            survived.append((rel, lineno, label, baseline_text[rel]))
        else:
            if out:
                log(f"  mutant diagnostics:\n{out}")
            killed += 1
        log(f"[{n}/{len(mutants)}] {rel}:{lineno} {label} {status} {dt:.1f}s")
    return total, killed, survived


def report_survivors(survived, log):
    for rel, lineno, label, source in survived:
        lines = source.splitlines()
        line = lines[lineno - 1].strip() if lineno <= len(lines) else ""
        log(f"survived: {rel}:{lineno} {label}  |  {line[:120]}")


def changed_targets(base_ref):
    out = subprocess.run(["git", "diff", "--name-only", f"{base_ref}...HEAD"],
                         cwd=REPO, capture_output=True, text=True, check=True)
    targets = []
    for name in out.stdout.splitlines():
        if (name.startswith("clipshelf/") and name.endswith(".py")
                and not Path(name).name.startswith("test")
                and not any(part in name for part in EXCLUDED_PARTS)):
            targets.append(name)
    return targets


def run(args, log=print):
    if args.diff:
        targets = changed_targets(args.diff)
        if not targets:
            log(f"no in-scope changed modules vs {args.diff}")
            return 0
    else:
        targets = args.targets or DEFAULT_TARGETS
    missing = [t for t in targets if not (REPO / t).exists()]
    if missing:
        log(f"target(s) not found: {', '.join(missing)}")
        return 2
    run_id = time.strftime("%Y%m%d-%H%M%S")
    temp_root = TEMP_ROOT / f"run-{run_id}"
    ws = temp_root / "workspace"
    log(f"workspace: {ws} (kept after the run; nothing is deleted)")
    shutil.copytree(REPO, ws, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "data", "android", "node_modules",
        ".worktrees", "htmlcov", ".pytest_cache", ".ruff_cache", "clipshelf-mutation",
        ".secrets", ".env*", "docs", "library.json", "server.log"),
        dirs_exist_ok=True)
    env = dict(os.environ)
    env["CLIPSHELF_DATA_DIR"] = str(temp_root / "data-baseline")
    env["CLIPSHELF_DEBUG"] = "1"
    log(f"baseline: {' '.join(TEST_CMD)}")
    code, out = run_suite(ws, env, TEST_CMD, args.timeout)
    if code != 0:
        log(f"baseline suite fails (exit {code}); mutation scores are meaningless:\n{out}")
        return 2
    total, killed, survived = score_run(targets, ws, temp_root, args.timeout,
                                        args.max_mutants, log)
    score = 100.0 * killed / total if total else 0.0
    log(f"\nscore: {killed}/{total} killed = {score:.1f}% (gate: >= {args.fail_under}%)")
    report_survivors(survived, log)
    log(f"kept (not deleted): {temp_root}")
    return 0 if score >= args.fail_under else 1


def self_check():
    """Assert the mutator produces the expected classes and that the runner
    kills a caught mutant and reports an uncaught one. Hermetic: its own
    temp workspace, plain unittest, no repo code."""
    global run_suite
    snippet = "def f(a, b):\n    if a == b:\n        return a + b\n    return a * b\n"
    labels = [label for _, label, _ in collect_sites(ast.parse(snippet))[1]]
    for expect in ("Eq->NotEq", "Add->Sub", "Mult->Div"):
        assert expect in labels, (expect, labels)
    assert not any("str->" in label for label in labels), labels  # no docstring noise

    report_source = "def sample():\n    return 1  # original formatting\n"

    ws = TEMP_ROOT / f"selfcheck-{time.strftime('%Y%m%d-%H%M%S')}" / "ws"
    ws.mkdir(parents=True)
    (ws / "report.py").write_text(report_source, encoding="utf-8")
    real_run_suite = run_suite
    try:
        run_suite = lambda *args: (0, "")
        _, _, survived = score_run(["report.py"], ws, ws, 60, 1, lambda _msg: None)
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
    code, out = run_suite(
        ws, dict(os.environ),
        [sys.executable, "-c",
         "import sys; print('stderr diagnostic', file=sys.stderr); sys.exit(1)"], 60)
    assert code == 1 and "stderr diagnostic" in out, out
    print(f"self-check ok ({ws})")


def main():
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
