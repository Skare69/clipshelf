"""Pure URL/tag/page-text helpers shared by acquisition and interpretation.

No I/O, no network: persistence and fetching live in the Django app
(core services) and clipshelf.network respectively.
"""
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

URL_RE = r"https?://[^\s\"'<>)\]]+"

# terminal hosts: catalogued with metadata, never sent for AI interpretation
TERMINAL = ("github.com", "gist.github.com", "huggingface.co", "arxiv.org",
            "colab.research.google.com")
# keywords that make a harvested <a> link worth keeping
KEYWORDS = ("prompt", "guide", "tutorial", "awesome", "cheatsheet", "workflow", "docs")
TRACKING = ("utm_", "fbclid", "gclid", "si=", "igsh")
# GitHub's own nav namespace: /features, /pricing/plans, ... are not repos
GH_RESERVED = frozenset((
    "features", "pricing", "topics", "collections", "trending", "marketplace",
    "sponsors", "about", "site", "orgs", "enterprise", "security", "customer-stories",
    "readme", "events", "explore", "settings", "notifications", "login", "join",
    "signup", "contact", "new", "why-github", "edu", "open-source"))
# link shorteners / share-redirects worth resolving before grouping
SHORT_HOSTS = ("share.google", "goo.gl", "g.co", "bit.ly", "tinyurl.com",
               "vm.tiktok.com", "vt.tiktok.com", "redd.it")


def norm(url):
    """Canonical URL or None: strips fragment, tracking params, trailing slash.

    Rejects non-http(s) schemes, credentials, malformed/oversized ports, and
    unbalanced IPv6 brackets so a hostile string can never become a stored
    entry key. IPv6 hosts stay bracketed so equivalent forms dedup.
    """
    try:
        s = urlsplit(url.strip().rstrip(".,;!").strip())
        if s.scheme not in ("http", "https") or not s.hostname:
            return None
        if s.username or s.password:
            return None
        port = s.port  # raises ValueError on a malformed port
        host = s.hostname.lower()
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        query = "&".join(p for p in s.query.split("&")
                         if p and not p.lower().startswith(TRACKING))
        path = s.path.rstrip("/")
        if host in ("github.com", "gist.github.com"):
            path = path.lower()  # github paths are case-insensitive; dedup on one form
        return urlunsplit((s.scheme, netloc, path, query, ""))
    except ValueError:
        return None


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


def is_short(url):
    """Shortened/share link that hides its real target behind a redirect?"""
    s = urlsplit(url)
    host = (s.hostname or "").removeprefix("www.")
    return (host in SHORT_HOSTS
            or (host == "reddit.com" and "/s/" in s.path)
            or (host == "tiktok.com" and s.path.startswith("/t/")))


def repo_url(r):
    """'owner/name', 'github.com/...', or full URL -> canonical repo URL."""
    r = r.strip()
    if r.startswith("github.com/"):  # scheme-less full path from a slide
        r = "https://" + r
    return norm(r if "://" in r else f"https://github.com/{r}")


TEXT_LIMIT = 100_000  # bounded readable body text handed to model input
# non-rendered containers whose contents are never readable page text
SKIP_TAGS = frozenset(("script", "style", "template", "noscript", "svg", "head"))
# block-level elements: closing one ends a text line
BLOCK = frozenset(("p", "div", "li", "ul", "ol", "tr", "table", "section", "article",
                   "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure",
                   "header", "footer", "main", "nav", "aside", "dl", "dt", "dd"))


class PageParser(HTMLParser):
    """Collects <title>, meta description, (href, anchor text) pairs, and a
    bounded readable body text (script/style/template/noscript/svg content
    excluded). Call close() to finalize .text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.links = "", "", []
        self._in_title, self._href, self._text = False, None, []
        self._skip, self._body, self.text = 0, [], ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta" and not self.desc:
            if a.get("name") == "description" or a.get("property") == "og:description":
                self.desc = a.get("content") or ""
        elif tag == "a" and a.get("href"):
            self._href, self._text = a["href"], []
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "br":
            self._body.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
        elif tag in BLOCK:
            self._body.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._href is not None:
            self._text.append(data)
        if not self._skip and len(self._body) < TEXT_LIMIT:
            self._body.append(data)

    def close(self):
        super().close()
        self.text = " ".join("".join(self._body).split())[:TEXT_LIMIT]
