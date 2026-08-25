"""Localhost server: library page, intake, installs, interpret pool."""
import json
import sys
import time

from . import pool as poolmod
from .lib import (CACHE, HERE, TODAY, list_dirs, load, norm, reinterpret,
                  remove_link, save, scan, split_dump)

def serve(port=8765):
    """Localhost intake server: GET / -> library.html, POST /add -> triage+enqueue.

    Queue is in-memory; links lost on Ctrl+C are not marked seen, so re-POSTing
    the same dump re-queues exactly the unscanned ones.
    """
    import http.server
    import os
    import queue as queue_mod
    import threading
    from datetime import datetime
    import subprocess

    if sys.stdout is None or sys.stderr is None:  # pythonw (scheduled task): no console
        sys.stdout = sys.stderr = open(HERE / "server.log", "a", 1, encoding="utf-8")
    print(f"serve: start pid={os.getpid()} at {datetime.now():%Y-%m-%d %H:%M:%S}")

    jobs = queue_mod.Queue()
    lock = threading.Lock()  # single-writer for library.json
    stat = {"current": "", "added": 0, "scanned": 0}

    def worker():
        while True:
            u = jobs.get()
            stat["current"] = u
            with lock:
                lib = load()
                stat["added"] += scan(lib, [u])
                stat["scanned"] += 1  # cumulative URLs processed (paste-box progress)
                save(lib)
            stat["current"] = ""

    threading.Thread(target=worker, daemon=True).start()
    pool = poolmod.Pool()
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # polling would spam the console
            pass

        def do_GET(self):
            if self.path == "/status":
                return self._send(200, json.dumps(
                    {"left": jobs.qsize() + (1 if stat["current"] else 0),
                     "current": stat["current"], "added": stat["added"],
                     "scanned": stat["scanned"]}
                ).encode("utf-8"), "application/json")
            if self.path == "/interpret/status":
                return self._send(200, json.dumps(pool.status())
                                  .encode("utf-8"), "application/json")
            if self.path.startswith("/cache/"):
                # trust boundary: basename only, no traversal out of cache/
                f = CACHE / Path(self.path).name
                if f.is_file():
                    ct = {"jpg": "image/jpeg", "mp4": "video/mp4"}.get(
                        f.suffix[1:], "application/octet-stream")
                    return self._send(200, f.read_bytes(), ct)
                return self._send(404, b"not found", "text/plain")
            page = HERE / "library.html"
            if self.path.split("?")[0] in ("/", "/library.html"):
                if not page.exists():
                    save(load())  # fresh clone: render the (empty) library once
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # trust boundary: a random webpage must not drive this server
            # (remove/install = RCE). Browsers send Origin on cross-site POSTs;
            # a DNS-rebinding form POST carries no Origin, so the Host header
            # is the backstop. Matching Origin against Host also lets
            # localhost:8765 work, not just 127.0.0.1.
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._send(403, b"unknown host", "text/plain")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                return self._send(403, b"cross-origin blocked", "text/plain")
            # video-embedded exports can be big; truncating here corrupts the JSON
            n = min(int(self.headers.get("Content-Length") or 0), 200_000_000)
            text = self.rfile.read(n).decode("utf-8", "replace")
            if self.path == "/add":
                with lock:
                    lib = load()
                    tiktok, fresh, known = split_dump(text, lib)
                for u in fresh:
                    jobs.put(u)
                return self._send(200, json.dumps(
                    {"tiktok": tiktok, "queued": len(fresh), "known": known,
                     "added": stat["added"], "scanned": stat["scanned"]}
                ).encode("utf-8"), "application/json")
            if self.path == "/remove":
                with lock:
                    lib = load()
                    ok = remove_link(lib, json.loads(text).get("url", ""))
                    save(lib)
                return self._send(200, json.dumps({"removed": ok})
                                  .encode("utf-8"), "application/json")
            if self.path == "/reinterpret":
                j = json.loads(text)
                u = None if j.get("all") else norm(j.get("url", ""))
                if not j.get("all") and not u:
                    return self._send(400, b"no url", "text/plain")
                with lock:
                    lib = load()
                    n = reinterpret(lib, u)
                    save(lib)
                return self._send(200, json.dumps({"flagged": n})
                                  .encode("utf-8"), "application/json")
            if self.path == "/dirs":
                try:
                    return self._send(200, json.dumps(list_dirs(
                        json.loads(text).get("path", ""))).encode("utf-8"),
                        "application/json")
                except OSError:
                    return self._send(404, b"not a folder", "text/plain")
            if self.path == "/install":
                j = json.loads(text)
                u = j.get("url", "")
                with lock:
                    cmd = load()["links"].get(u, {}).get("install")
                if not cmd:
                    return self._send(404, b"no install command", "text/plain")
                if j.get("dir"):
                    dest = Path(j["dir"]).expanduser()
                    if not dest.is_dir():
                        return self._send(400, b"not a folder", "text/plain")
                else:
                    dest = HERE / "installs"  # legacy default; pip/npm ignore cwd
                    dest.mkdir(exist_ok=True)
                code = self._stream_cmd(cmd, dest, 600)
                if code == 0:
                    with lock:
                        lib = load()
                        if u in lib["links"]:
                            lib["links"][u]["install_done"] = TODAY
                            lib["links"][u]["install_dir"] = str(dest)
                            save(lib)
                print(f"install {'ok' if code == 0 else 'FAIL'}: {cmd}")
                return self._tail(code)
            if self.path == "/ingest":
                j = json.loads(text)
                p = (j.get("path") or "").strip().strip('"')
                dl = Path(j.get("dir") or "~/Downloads").expanduser()
                if p:
                    f = Path(p) if Path(p).is_absolute() else dl / p
                else:  # blank: newest tiktok export in the chosen folder
                    c = sorted(dl.glob("tiktok*.json"), key=lambda x: x.stat().st_mtime)
                    f = c[-1] if c else None
                if not f or not f.exists():
                    return self._send(404, b"export file not found", "text/plain")
                # the CLI already does ingest+scan+re-render in one deterministic place
                code = self._stream_cmd(
                    f'"{sys.executable}" "{HERE / "clipshelf.py"}" "{f}"', HERE, 3600,
                    head=f"file: {f}\n\n")
                print(f"ingest {'ok' if code == 0 else 'FAIL'}: {f}")
                return self._tail(code)
            if self.path == "/settings":
                j = json.loads(text)
                with lock:
                    lib = load()
                    lib.setdefault("settings", {})["interpret_cmd"] = \
                        (j.get("interpret_cmd") or "").strip()
                    save(lib)
                return self._send(200, b'{"ok": true}', "application/json")
            if self.path == "/interpret":
                with lock:
                    pool.start(load())  # False if already running: follow that run
                return self._stream_pool()
            if self.path == "/interpret/stop":
                pool.stop_ev.set()
                return self._send(200, b'{"ok": true}', "application/json")
            self._send(404, b"not found", "text/plain")

        def _stream_cmd(self, cmd, cwd, cap, head=""):
            # octoprint-style terminal mirror: stream live output; _tail adds [exit N]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._gone, code = False, -1
            if head:
                self._say(head)
            try:
                # pythonw has no console: an inherited stdin handle is invalid and
                # kills omp mid-start; NUL also stops installers wedging on prompts
                p = subprocess.Popen(cmd, shell=True, cwd=cwd,
                                     stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
                watchdog = threading.Timer(cap, p.kill)  # hung prompt insurance
                watchdog.start()
                for line in p.stdout:
                    self._say(line)  # browser may be gone; command finishes anyway
                code = p.wait()
                watchdog.cancel()
            except Exception as exc:
                self._say(f"\n{exc}\n")
            return code

        def _say(self, s):
            if not self._gone:
                try:
                    self.wfile.write(s.encode("utf-8"))
                    self.wfile.flush()
                except OSError:
                    self._gone = True

        def _tail(self, code):
            self._say(f"\n[exit {code}]\n")

        def _stream_pool(self):
            """tail -f on the pool line buffer: replay, then live until [exit 0]."""
            self._gone = False
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = 0
            try:
                while True:
                    with pool.lock:
                        new = [(s, l) for s, l in pool.buf if s > last]
                        running = pool.running
                    for _, l in new:
                        self._say(l)
                    if new:
                        last = new[-1][0]
                    if not running and not new:
                        break
                    time.sleep(0.5)
            except (OSError, BrokenPipeError):
                pass  # client left; the pool keeps running server-side

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"clipshelf on http://127.0.0.1:{port} (Ctrl+C stops; queue is in-memory)")
    srv.serve_forever()
