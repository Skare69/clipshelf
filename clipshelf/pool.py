"""Interpret worker pool: one omp worker per pending entry, capped concurrency.

Runs server-side, independent of any open dialog: closing the page never kills
a run. Stream clients (the UI dialog, the header chip) tail the line buffer.
"""
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from .lib import HERE, interp_cmd

CAP = 4  # ponytail: concurrent workers; bump when the model host can take more
TIMEOUT = 900  # per-entry wall-clock seconds before kill

INTERP_DIR = HERE / "_interp"
LOG_DIR = INTERP_DIR / "logs"
MERGED = INTERP_DIR / "findings_merged.json"
BUF_MAX = 500  # ring buffer of stream lines (replay on (re)connect)

PROMPT = ("Interpret the single clipshelf entry at {entry} (a JSON file with "
          "url, desc, and image/video/cache paths). Follow the clipshelf-interpret "
          "skill, single-entry mode: write exactly one findings entry as a JSON "
          "array to {out}. Do not run add-extracted, do not edit library.json, "
          "do not delete files.")


def entry_hash(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class Pool:
    def __init__(self):
        self.lock = threading.RLock()  # _run emits while holding it
        self.stop_ev = threading.Event()
        self.running = False
        self.total = 0
        self.done = 0      # entries completed (done + failed + timeout + stopped)
        self.failed = 0
        self.model = ""
        self.merged = 0
        self.started = 0.0
        self.seq = 0
        self.buf = []      # [(seq, line)]
        self.clients = 0   # live stream connections (bookkeeping for logs)

    # -- streaming ----------------------------------------------------------
    def emit(self, line):
        line = line if line.endswith("\n") else line + "\n"
        with self.lock:
            self.seq += 1
            self.buf.append((self.seq, line))
            if len(self.buf) > BUF_MAX:
                self.buf.pop(0)

    def status(self):
        with self.lock:
            return {"running": self.running, "done": self.done, "total": self.total,
                    "failed": self.failed, "model": self.model, "merged": self.merged,
                    "elapsed": int(time.time() - self.started) if self.running else 0,
                    "buf": [l for _, l in self.buf[-80:]]}

    # -- run ------------------------------------------------------------------
    def start(self, lib):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.done = self.failed = self.merged = 0
            self.total = 0
            self.stop_ev.clear()
            self.started = time.time()
            self.buf = []
            self.seq = 0
        threading.Thread(target=self._run, args=(lib,), daemon=True).start()
        return True

    def _run(self, lib):
        try:
            items = [(u, e) for u, e in lib["links"].items() if e.get("pending")]
            base = interp_cmd(lib)
            self.model = base.split("--model")[-1].strip() if "--model" in base else "default"
            self.total = len(items)
            self.emit(f"command: {base}\n")
            if not items:
                self.emit("nothing pending\n")
                return
            INTERP_DIR.mkdir(exist_ok=True)
            LOG_DIR.mkdir(exist_ok=True)
            todo = []
            for u, e in items:
                h = entry_hash(u)
                (INTERP_DIR / f"entry_{h}.json").write_text(
                    json.dumps({"url": u, "desc": e.get("desc", ""),
                                "images": e.get("images", []), "video": e.get("video"),
                                "cache": e.get("cache")}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                todo.append((h, u))
            self.emit(f"total: {len(todo)}\n")

            procs = {}  # h -> [Popen, start_time, url, logfile]
            ni = 0
            settled = 0  # settled worker count (for stream numbering)
            while True:
                with self.lock:
                    # launch up to CAP pending entries
                    while len(procs) < CAP and ni < len(todo) and not self.stop_ev.is_set():
                        h, u = todo[ni]
                        ni += 1
                        lf = open(LOG_DIR / f"{h}.log", "w", encoding="utf-8")
                        prompt = PROMPT.format(entry=f"_interp/entry_{h}.json",
                                               out=f"_interp/findings_{h}.json")
                        p = subprocess.Popen(f'{base} "{prompt}"', shell=True,
                                             cwd=HERE, stdin=subprocess.DEVNULL,
                                             stdout=lf, stderr=subprocess.STDOUT)
                        procs[h] = [p, time.time(), u, lf]
                        self.emit(f"[{self.done + len(procs)}/{self.total}] started {u}\n")
                    if not procs and (ni >= len(todo) or self.stop_ev.is_set()):
                        if self.stop_ev.is_set():
                            self.emit(f"stopped: {self.done}/{self.total} complete\n")
                        break
                # wait ~1s, then reap exited/timed-out workers
                time.sleep(1.0)
                for h, item in list(procs.items()):
                    p, t0, u, lf = item
                    if p.poll() is None and (self.stop_ev.is_set()
                                             or time.time() - t0 > TIMEOUT):
                        p.kill()
                    if p.poll() is not None:
                        lf.close()
                        ok = p.returncode == 0
                        killed = (not ok) and self.stop_ev.is_set()
                        settled += 1
                        if not ok and not killed:
                            self.failed += 1
                        if not killed:
                            self.done += 1
                        self.emit(f"[{settled}/{self.total}] "
                                  f"{'stopped' if killed else ('done' if ok else 'failed')} {u}\n")
                        del procs[h]
            self._merge()
        finally:
            with self.lock:
                self.running = False
            self.emit("[exit 0]\n")

    def _merge(self):
        """Consolidate findings_<hash>.json and run add-extracted once, streaming it."""
        findings = []
        for f in sorted(INTERP_DIR.glob("findings_*.json")):
            if f.name == MERGED.name:  # don't re-consume our own output
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, list):
                findings.extend(data)
            f.unlink(missing_ok=True)  # consumed
        if not findings:
            self.emit("merge: 0 findings\n")
            return
        MERGED.write_text(json.dumps(findings, indent=1, ensure_ascii=False),
                          encoding="utf-8")
        self.emit(f"merge: {len(findings)} findings\n")
        cmd = (f'"{sys.executable}" "{HERE / "clipshelf.py"}" '
               f'add-extracted "{MERGED}"')
        p = subprocess.Popen(cmd, shell=True, cwd=HERE, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        for line in p.stdout:
            self.emit(line)
        p.wait()
        self.merged = len(findings)
        print(f"interpret merged {self.merged} findings, "
              f"{self.failed} failed of {self.total}")
