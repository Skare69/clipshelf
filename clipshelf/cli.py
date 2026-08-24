"""clipshelf CLI.

Usage:
  python clipshelf.py links.txt            # ingest: any text file, URLs regexed out
  python clipshelf.py triage dump.txt      # sort a raw link dump by domain,
                                           # resolving share.google/vm.tiktok/... redirects
  echo https://github.com/x/y | python clipshelf.py -
  python clipshelf.py pending              # JSON list of items awaiting interpretation
  python clipshelf.py add-extracted f.json # merge findings (see .claude/skills/clipshelf-interpret)
  python clipshelf.py serve [port]         # localhost server: paste box POSTs dumps,
                                           # background worker ingests (default port 8765)

Data: library.json (source of truth), cache/ (raw pages + images), library.html (browse).
Seen URLs are skipped; failed fetches are NOT marked seen, so a rerun retries them.
Stdlib only.
"""
import json
import re
import sys
from pathlib import Path

from .lib import (URL_RE, add_extracted, ingest_tiktok, load, norm,
                  refresh, repo_url, save, scan, triage)
from .server import serve

def main(argv):
    if argv[:1] == ["serve"]:
        serve(int(argv[1]) if argv[1:] else 8765)
        return
    lib = load()
    if argv[:1] == ["pending"]:
        print(json.dumps(
            [{"url": u, "desc": e["desc"], "images": e.get("images", []),
              "video": e.get("video"), "cache": e.get("cache")}
             for u, e in lib["links"].items() if e.get("pending")],
            indent=1, ensure_ascii=False))
        return
    if argv[:1] == ["refresh"]:
        refresh(lib)
        save(lib)
        return
    if argv[:1] == ["triage"]:
        text = " ".join(sys.stdin.read() if a == "-" else Path(a).read_text(encoding="utf-8")
                        for a in (argv[1:] or ["-"]))
        for host, urls in sorted(triage(text, lib).items(), key=lambda g: -len(g[1])):
            fresh = [u for u, seen in urls if not seen]
            old = len(urls) - len(fresh)
            print(f"\n{host} ({len(fresh)} new" + (f", {old} in library" if old else "") + ")")
            for u in fresh:
                print(" ", u)
        return
    if argv[:1] == ["categorize"]:
        cats = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        hits = 0
        for key, cat in cats.items():
            u = repo_url(key) or norm(key)
            e = (lib["links"].get(u) if u else None) or lib["prompts"].get(key)
            if e:
                e["cat"] = cat
                hits += 1
            else:
                print("no entry:", key)
        save(lib)
        print(f"{hits} categorized -> library.html")
        return
    if argv[:1] == ["add-extracted"]:
        findings = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        queue = add_extracted(lib, findings)
    else:
        queue = []
        for arg in argv or ["-"]:
            raw = (sys.stdin.read() if arg == "-"
                   else Path(arg).read_text(encoding="utf-8"))
            if arg.endswith(".json"):
                queue += ingest_tiktok(json.loads(raw), lib)
            else:
                queue += re.findall(URL_RE, raw)
    new = scan(lib, queue)
    save(lib)
    pending = sum(1 for e in lib["links"].values() if e.get("pending"))
    print(f"{new} scanned, {len(lib['links'])} links, {len(lib['prompts'])} prompts, "
          f"{pending} pending -> library.html")
