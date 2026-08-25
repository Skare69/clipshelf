"""clipshelf core: library data, URL rules, fetch/scan, findings merge."""
import base64
import hashlib
import json
import re
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parents[1]
LIB = HERE / "library.json"
CACHE = HERE / "cache"
TODAY = date.today().isoformat()
URL_RE = r"https?://[^\s\"'<>)\]]+"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) clipshelf/0.1"}

# terminal hosts: catalogued with metadata, never sent for AI interpretation
TERMINAL = ("github.com", "gist.github.com", "huggingface.co", "arxiv.org",
            "colab.research.google.com")
# keywords that make a harvested <a> link worth keeping
KEYWORDS = ("prompt", "guide", "tutorial", "awesome", "cheatsheet", "workflow", "docs")
TRACKING = ("utm_", "fbclid", "gclid", "si=", "igsh")
# GitHub's own nav namespace: /features, /pricing/plans, ... are not repos
GH_RESERVED = frozenset((
    "features", "solutions", "site", "resources", "enterprise", "topics",
    "collections", "sponsors", "marketplace", "orgs", "organizations", "apps",
    "settings", "about", "pricing", "premium-support", "customer-stories",
    "security", "trending", "events", "mobile", "readme", "join", "login",
    "signup", "contact", "new", "why-github", "edu", "open-source"))
# link shorteners / share-redirects worth resolving before grouping
SHORT_HOSTS = ("share.google", "goo.gl", "g.co", "bit.ly", "tinyurl.com",
               "vm.tiktok.com", "vt.tiktok.com", "redd.it")
def norm(url):
    """Canonical URL or None: strips fragment, tracking params, trailing slash."""
    s = urlsplit(url.strip().rstrip(".,;!"))
    if s.scheme not in ("http", "https") or not s.hostname:
        return None
    query = "&".join(p for p in s.query.split("&")
                     if p and not p.lower().startswith(TRACKING))
    host = s.hostname.lower() + (f":{s.port}" if s.port else "")
    path = s.path.rstrip("/")
    if host in ("github.com", "gist.github.com"):
        path = path.lower()  # github paths are case-insensitive; dedup on one form
    return urlunsplit((s.scheme, host, path, query, ""))


def is_terminal(url):
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return host in TERMINAL


def github_repo(url):
    """True for repo-shaped github/gist URLs (owner/name), not GitHub nav chrome."""
    s = urlsplit(url)
    host = (s.hostname or "").removeprefix("www.")
    if host not in ("github.com", "gist.github.com"):
        return False
    parts = [p for p in s.path.split("/") if p]
    return len(parts) == 2 and parts[0].lower() not in GH_RESERVED


def tag_for(url, text=""):
    """Tag string if url/anchor-text looks worth keeping, else None."""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    if host.endswith("github.com"):  # docs./archiveprogram./... are chrome, not content
        return "github" if github_repo(url) else None
    if is_terminal(url):
        return host.split(".")[0]
    blob = (url + " " + text).lower()
    return next((k for k in KEYWORDS if k in blob), None)


def cache_bytes(data, ext):
    """Write bytes to cache/<sha1>.<ext>; return repo-relative path."""
    CACHE.mkdir(exist_ok=True)
    name = f"{hashlib.sha1(data).hexdigest()[:16]}.{ext}"
    (CACHE / name).write_bytes(data)
    return f"cache/{name}"


class PageParser(HTMLParser):
    """Collects <title>, meta description, and (href, anchor text) pairs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.links = "", "", []
        self._in_title, self._href, self._text = False, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and not self.desc:
            if a.get("name") == "description" or a.get("property") == "og:description":
                self.desc = a.get("content") or ""
        elif tag == "a" and a.get("href"):
            self._href, self._text = a["href"], []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._text.append(data)


def fetch(url):
    """(page text or None, final URL after redirects)."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        final = norm(r.geturl()) or url
        if r.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
            return None, final
        return r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8",
                                        "replace"), final


def is_short(url):
    """Shortened/share link that hides its real target behind a redirect?"""
    s = urlsplit(url)
    host = (s.hostname or "").removeprefix("www.")
    return (host in SHORT_HOSTS
            or (host == "reddit.com" and "/s/" in s.path)
            or (host == "tiktok.com" and s.path.startswith("/t/")))


def resolve(url):
    """Final URL after redirects; the input URL if resolution fails."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=UA, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.url
        except Exception:
            continue
    return url


def triage(text, lib):
    """Group URLs from a text dump by host: {host: [(url, seen_bool)]}."""
    groups = {}
    done = set()
    for raw in re.findall(URL_RE, text):
        u = norm(raw)
        if not u or u in done:
            continue
        done.add(u)
        if is_short(u):
            r = norm(resolve(u))
            if r and r != u:
                print("resolved:", u, "->", r)
                if r in done:
                    continue
                done.add(r)
                u = r
        host = (urlsplit(u).hostname or "?").removeprefix("www.")
        groups.setdefault(host, []).append((u, u in lib["seen"]))
    return groups


def split_dump(text, lib):
    """Triage a raw dump for the server: (tiktok urls, fresh urls to scan, known)."""
    tiktok, fresh, known = [], [], 0
    for host, urls in triage(text, lib).items():
        for u, seen in urls:
            if seen:
                known += 1
            elif host == "tiktok.com" or host.endswith(".tiktok.com"):
                tiktok.append(u)  # bot-blocked: goes through the browser panel
            else:
                fresh.append(u)
    return tiktok, fresh, known
def add_link(lib, url, title, desc, source, tags):
    if url in lib.setdefault("removed", {}):  # tombstoned: mutate a throwaway so callers are unaware
        return {"title": "", "desc": "", "sources": [], "tags": [], "found": TODAY}
    e = lib["links"].setdefault(
        url, {"title": "", "desc": "", "sources": [], "tags": [], "found": TODAY})
    if title and len(title.strip()) > len(e["title"]):
        e["title"] = " ".join(title.split())
    if desc and len(desc) > len(e["desc"]):
        e["desc"] = " ".join(desc.split())
    e["sources"] = sorted(set(e["sources"]) | ({source} if source else set()))
    e["tags"] = sorted(set(e["tags"]) | {t for t in tags if t})
    return e


def remove_link(lib, url):
    """Delete a link and tombstone it so no future ingest re-adds it."""
    u = norm(url) or url
    lib.setdefault("removed", {})[u] = TODAY
    return lib["links"].pop(u, None) is not None


def list_dirs(path):
    """Folder listing for the install picker; empty path = the installs/ default."""
    p = Path(path).expanduser() if path else HERE / "installs"
    if not path:
        p.mkdir(exist_ok=True)
    p = p.resolve()
    dirs = sorted((d.name for d in p.iterdir()
                   if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "dirs": dirs[:200]}


def reinterpret(lib, url=None):
    """Re-flag entries with cached content so the next interpret re-reads them
    with current rules (new extractors, prompt cats, whatever comes later)."""
    n = 0
    for u, e in lib["links"].items():
        if (url is None or u == url) and (
                e.get("images") or e.get("video") or e.get("cache")):
            e["pending"] = True
            n += 1
    return n


def add_prompt(lib, title, text, source, cat=""):
    key = hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()[:16]
    p = lib["prompts"].setdefault(
        key, {"title": "", "text": text.strip(), "sources": [], "found": TODAY})
    if title and len(title) > len(p["title"]):
        p["title"] = title
    if cat and not p.get("cat"):
        p["cat"] = cat
    p["sources"] = sorted(set(p["sources"]) | {source})
    return p


def ingest_tiktok(items, lib):
    """Ingest tiktok-extract.js export; returns URLs found in descriptions."""
    queue = []
    for i, it in enumerate(items, 1):
        print(f"import {i}/{len(items)}", it.get("url", "?"))
        if it.get("error"):
            print("skip (export error):", it.get("url"), "-", it["error"])
            continue
        if it.get("passthrough"):  # non-tiktok link that rode along in the dump
            queue.append(it["url"])
            continue
        u = norm(it.get("resolvedUrl") or it.get("url", ""))
        if not u or u in lib["seen"]:
            continue
        lib["seen"][u] = TODAY
        orig = norm(it.get("url", ""))
        if orig:  # shortlink counts as seen too
            lib["seen"].setdefault(orig, TODAY)
        e = add_link(lib, u, f"@{it.get('author', '?')} on tiktok",
                     it.get("desc", ""), "tiktok-export", ["tiktok"])
        e["images"] = []
        for im in it.get("images", []):
            iu = im.get("url", "")
            if "-avt-" in iu or "cropcenter:100:100" in iu:
                continue  # avatar / feed thumbnail, not a slide
            if im.get("b64"):
                e["images"].append(cache_bytes(base64.b64decode(im["b64"]), "jpg"))
            elif im.get("url"):  # browser couldn't read bytes; fetch fresh URL now
                try:
                    req = urllib.request.Request(im["url"], headers=UA)
                    e["images"].append(cache_bytes(
                        urllib.request.urlopen(req, timeout=20).read(), "jpg"))
                except Exception as exc:
                    print("  image fetch fail:", it.get("url"), "-", exc)
        v = it.get("video") or {}
        raw = base64.b64decode(v["b64"]) if v.get("b64") else None
        if raw is not None and raw[4:8] != b"ftyp":
            # trust boundary: CDNs answer 403/error pages as 200s
            print("  video junk (CDN error body), trying mirrors:", it.get("url"))
            raw = None
        if raw is None:  # browser couldn't read bytes; try each mirror fresh
            for vu in dict.fromkeys([v.get("url"), *v.get("urls", [])]):
                if not vu:
                    continue
                try:
                    req = urllib.request.Request(vu, headers=UA)
                    cand = urllib.request.urlopen(req, timeout=60).read()
                except Exception as exc:
                    print("  video fetch fail:", vu[:60], "-", exc)
                    continue
                if cand[4:8] == b"ftyp":
                    raw = cand
                    break
                print("  video junk (CDN error body), next mirror:", vu[:60])
        if raw is not None:
            e["video"] = cache_bytes(raw, "mp4")
        if e["images"] or e.get("video"):
            e["pending"] = True
        queue += re.findall(URL_RE, it.get("desc", ""))
    return queue


def merge_rename(lib, u, final):
    """Renamed repo / moved page: re-home the entry under the target URL."""
    lib["seen"][final] = TODAY
    old = lib["links"].pop(u)
    tgt = lib["links"].get(final)
    if tgt:
        tgt["sources"] = sorted(set(tgt["sources"]) | set(old["sources"]))
        tgt["tags"] = sorted(set(tgt["tags"]) | set(old["tags"]))
        if old.get("cat") and not tgt.get("cat"):
            tgt["cat"] = old["cat"]
    else:
        lib["links"][final] = old
    return final, lib["links"][final]


def catalogue(lib, u, e, page, source=""):
    """Cache a fetched page and update the entry's metadata from it."""
    e["cache"] = cache_bytes(page.encode("utf-8"), "html")
    p = PageParser()
    p.feed(page)
    stars = re.search(r'"stargazerCount":\s*(\d+)', page)
    if stars:
        e["stars"] = int(stars.group(1))
    if re.search(r'"isArchived":\s*true', page) or "has been archived by the owner" in page:
        e["archived"] = TODAY
    else:
        e.pop("archived", None)  # un-archived since last fetch
    add_link(lib, u, p.title, p.desc, source, [tag_for(u, p.title) or "page"])
    return p


def scan(lib, queue):
    """Fetch unseen URLs: cache page, catalogue it, harvest interesting links."""
    todo = list(dict.fromkeys(
        u for u in (norm(r) for r in queue) if u and u not in lib["seen"]))
    new = 0
    for i, u in enumerate(todo, 1):
        if u in lib["seen"]:  # redirect target already reached earlier this run
            continue
        print(f"scan {i}/{len(todo)}", u)
        try:
            page, final = fetch(u)
        except Exception as exc:
            print("  fail:", exc)  # not marked seen -> retried next run
            continue
        lib["seen"][u] = TODAY
        new += 1
        e = add_link(lib, u, "", "", "input", [tag_for(u) or "page"])
        if final != u:  # renamed repo / moved page: catalogue under the target
            u, e = merge_rename(lib, u, final)
        if page is None:
            continue
        p = catalogue(lib, u, e, page, "input")
        if not is_terminal(u):
            e["pending"] = True  # AI reads the cached page via the interpret skill
            # harvest links only from content pages; a repo page's links are chrome
            for href, text in p.links:
                full = norm(urljoin(u, href))
                t = tag_for(full, text) if full else None
                if t and full != u:
                    add_link(lib, full, text, "", u, [t])
    return new


def refresh(lib):
    """Re-fetch every catalogued link: update title, stars, archived, cache.

    Never re-flags pending (no re-interpretation) and adds no sources.
    Skips tiktok links - their content is already saved locally and the
    logged-out page is a bot wall that would pollute titles.
    """
    ok = fail = 0
    todo = [u for u in lib["links"]
            if not (urlsplit(u).hostname or "").endswith("tiktok.com")]
    for i, u in enumerate(todo, 1):
        e = lib["links"].get(u)
        if e is None:  # merged into a rename target earlier this run
            continue
        print(f"refresh {i}/{len(todo)}", u)
        try:
            page, final = fetch(u)
        except Exception as exc:
            fail += 1
            print("  fail:", exc)
            continue
        ok += 1
        lib["seen"][u] = TODAY
        if final != u:
            u, e = merge_rename(lib, u, final)
        if page is not None:
            catalogue(lib, u, e, page)
    print(f"refreshed {ok}, {fail} failed")


def repo_url(r):
    """'owner/name', 'github.com/...', or full URL -> canonical repo URL."""
    r = r.strip()
    if r.startswith("github.com/"):  # scheme-less full path from a slide
        r = "https://" + r
    return norm(r if "://" in r else f"https://github.com/{r}")


SKILL_ROOTS = [Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills",
               Path.home() / ".omp" / "agent" / "managed-skills"]
PLUGINS = Path.home() / ".claude" / "plugins"


def installed_index():
    """Lowercase skill dir names, plugin names, and 'owner/repo' -> install location."""
    idx = {}
    for root in SKILL_ROOTS:
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir():
                    idx[d.name.lower()] = "skill: " + str(d).replace(str(Path.home()), "~")
    try:
        mk = json.loads((PLUGINS / "known_marketplaces.json").read_text(encoding="utf-8"))
        for v in mk.values():
            repo = (v.get("source") or {}).get("repo", "")
            if repo:
                idx[repo.lower()] = "claude plugin marketplace"
        pl = json.loads((PLUGINS / "installed_plugins.json").read_text(encoding="utf-8"))
        for k in pl.get("plugins", {}):
            idx[k.split("@")[0].lower()] = "claude plugin"
    except (OSError, ValueError):
        pass
    return idx


def installed_map(lib, idx=None):
    """lib url -> install location, for github repos matching a local skill/plugin.

    ponytail: name match is a heuristic - a repo shipping many skills under other
    names won't match; the badge tooltip says which local thing matched and where.
    """
    idx = installed_index() if idx is None else idx
    out = {}
    for u in lib["links"]:
        parts = urlsplit(u)
        if parts.hostname != "github.com":
            continue
        seg = parts.path.strip("/").split("/")
        if len(seg) < 2:
            continue
        hit = idx.get(f"{seg[0]}/{seg[1]}") or idx.get(seg[1])
        if hit:
            out[u] = hit
    return out


def add_extracted(lib, findings):
    """Merge interpret-skill findings; returns new URLs to scan for metadata."""
    queue = []
    for f in findings:
        src = norm(f.get("source", "")) or f.get("source", "?")
        entry = lib["links"].get(src)
        if entry:
            entry.pop("pending", None)
            entry["interpreted"] = TODAY  # newest-first sort key; refresh on re-interpret
            if f.get("summary") and len(f["summary"]) > len(entry["desc"]):
                entry["desc"] = f["summary"]
        cats = f.get("categories", {})
        installs = f.get("installs", {})
        for repo in f.get("repos", []):
            u = repo_url(repo)
            if u:
                e = add_link(lib, u, "", "", src, ["github"])
                e["interpreted"] = TODAY
                if cats.get(repo):
                    e["cat"] = cats[repo]
                if installs.get(repo):
                    e["install"] = installs[repo]
                queue.append(u)
        for link in f.get("links", []):
            u = norm(link.get("url", ""))
            if u:
                e = add_link(lib, u, link.get("title", ""), "", src,
                         [tag_for(u, link.get("title", "")) or "page"])
                e["interpreted"] = TODAY
                queue.append(u)
        for pr in f.get("prompts", []):
            if (pr.get("text") or "").strip():
                p = add_prompt(lib, pr.get("title", ""), pr["text"], src, pr.get("cat", ""))
                p["interpreted"] = TODAY
    return queue

HTML = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")

# full-command form saved before interpret_cmd became a base command
_LEGACY_CMD = ('omp -p "Interpret pending clipshelf items with the '
                'clipshelf-interpret skill." --model llama.cpp/qwen3.8-27b')

INTERP_CMD = "omp -p --model llama.cpp/qwen3.8-27b"

def interp_cmd(lib):
    """Settings override for the interpretation command; blank = built-in omp."""
    return (lib.get("settings", {}).get("interpret_cmd") or "").strip() or INTERP_CMD

def load():
    lib = (json.loads(LIB.read_text(encoding="utf-8")) if LIB.exists()
           else {"seen": {}, "links": {}, "prompts": {}})
    lib.setdefault("removed", {})  # tombstones: urls never to re-add
    c = lib.get("settings", {}).get("interpret_cmd", "").strip()
    if c == _LEGACY_CMD:  # one-time migration: full command -> base command
        lib["settings"]["interpret_cmd"] = ""
        save(lib)
    return lib

def save(lib):
    LIB.write_text(json.dumps(lib, indent=1, ensure_ascii=False), encoding="utf-8")
    (HERE / "library.html").write_text(
        HTML.replace("__DATA__", json.dumps({**lib, "installed": installed_map(lib)},
                                            ensure_ascii=False)
                     .replace("</", "<\\/"))  # keep embedded HTML out of the <script>
            .replace("__IDEF__", json.dumps(INTERP_CMD)),
        encoding="utf-8")
