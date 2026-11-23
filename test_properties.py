"""Property checks for the trust-boundary parsers: python test_properties.py.

Stdlib seeded fuzz loop — no new dependencies. Fixed seed, 150 runs per
property; the review-found counterexamples run first as explicit examples.
Offline: DNS is faked, and the feed property talks to a throwaway in-memory
test database, never the real one. Each property states one rule
that must hold for every input (idempotence, confinement, 4xx-never-500,
parser agreement, reference agreement); a failure prints the seed and input.

Reviews rerun the suite with a random seed and far more runs, without
editing this file:
  CLIPSHELF_PROPERTY_SEED=<random> CLIPSHELF_PROPERTY_RUNS=15000 \
      python3 test_properties.py
"""
import contextlib
import io
import ipaddress
import json
import os
import random
import re
import socket
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, urlsplit

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")
os.environ.setdefault("CLIPSHELF_DATA_DIR",
                      tempfile.mkdtemp(prefix="clipshelf-properties-"))
os.environ.setdefault("CLIPSHELF_DEBUG", "1")

import django

django.setup()

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.test import runner as test_runner
from django.test import utils as test_utils

from clipshelf import acquisition, follows, imports, interpretation
from clipshelf import lib, models, network, services
from clipshelf.api import ApiError, json_body


def _env_int(name, default):
    """Optional integer override for the review runs; bad values stop now."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, not {raw!r}")


SEED = _env_int("CLIPSHELF_PROPERTY_SEED", 20261014)
RUNS = _env_int("CLIPSHELF_PROPERTY_RUNS", 150)
if RUNS < 1:
    raise SystemExit("CLIPSHELF_PROPERTY_RUNS must be at least 1")
MAX_BODY_BYTES = 64 * 1024

# chars that render as blank space but count as letters; combining marks and
# control/zero-width chars are the other invisibles the label rule must drop
INVISIBLE = "\u115f\u1160\u3164\uffa0\u0301\u0302\u200b\u00ad\u0000"


def visible_label(text):
    """Independent restatement of the label rule: short and visibly lettered."""
    return (0 < len(text) <= 40 and text.isprintable()
            and any(ch.isalnum() and ch not in INVISIBLE for ch in text))


def rand_json(rng, depth=0):
    """JSON-shaped value with hostile strings mixed in (the real input domain)."""
    if depth >= 3 or rng.random() < 0.35:
        return rng.choice([
            "", "ok", "https://example.com/a?b=c", "\ud800", "\u0000",
            "\u200b", "\u3164", "\u0301\u0302", "caf\u00e9", "\U0001F600",
            "\t", "x" * 300, 0, 1, -2.5, True, False, None,
        ])
    if rng.random() < 0.5:
        keys = rng.sample(
            ["summary", "repos", "links", "categories", "prompts", "installs",
             "warnings", "seen", "removed", "a", "b", ""], rng.randint(1, 4))
        return {k: rand_json(rng, depth + 1) for k in keys}
    return [rand_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]


def rand_upload(rng):
    """Upload bodies: well-formed shapes (with hostile values) plus garbage."""
    kind = rng.random()
    if kind < 0.35:
        return json.dumps([
            {"url": f"https://example.com/v{n}", "title": rand_json(rng, 2)}
            for n in range(rng.randint(1, 3))
        ]).encode("utf-8")
    if kind < 0.65:
        return json.dumps({
            "links": {f"u{n}": {"title": rand_json(rng, 2)}
                      for n in range(rng.randint(0, 3))},
            "prompts": rand_json(rng, 2),
        }).encode("utf-8")
    if kind < 0.8:
        return json.dumps(rand_json(rng)).encode("utf-8")
    if kind < 0.9:
        return b"".join(bytes([rng.randint(0, 255)]) for _ in range(rng.randint(0, 40)))
    return rng.choice((
        b'\xef\xbb\xbf[{"a":1}]',
        '[{"a":1}]'.encode("utf-16"),
        b'[' * 100000,
        b'{"links":{"u":' + json.dumps(rand_json(rng, 1)).encode("utf-8") + b'}}',
    ))


# --------------------------------------------------------------- 1. lib.norm

def prop_norm(rng):
    """norm is idempotent and never emits credentials or a non-http(s) scheme."""
    alphabet = "abcABC019:/?#[]@!$&'()*+,;=-._~%é\ud800\udcff\t <>\"\\|"
    corpus = [
        "https://User:pw@GitHub.com/Owner/Repo/",
        "http://EXAMPLE.com", "https://example.com:99999/", "https://[::1]/",
        "https://example.com/path?utm_source=x&keep=1",
        "gopher://example.com/x", "https://example.com/path#frag",
        "https://example.com", "", "https://",
    ] + ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
         for _ in range(RUNS)]
    bad = []
    for url in corpus:
        once = lib.norm(url)
        if once is None:
            continue
        if lib.norm(once) != once:
            bad.append(("not idempotent", url, once))
        parts = urlsplit(once)
        if parts.scheme not in ("http", "https"):
            bad.append(("scheme leaked", url, once))
        if parts.username or parts.password:
            bad.append(("credentials leaked", url, once))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# --------------------------------------------- 2. imports._legacy_media_path

def prop_legacy_media_path(rng):
    """Accepted references stay inside the cache root; shape-invalid raises.

    Reference semantics from the docstrings: a drive, a root, or a '..' part
    must raise ValidationError, and a path that resolves outside the root
    must never come back as a file.
    """
    bad = []
    with tempfile.TemporaryDirectory() as outer:
        root = Path(outer) / "cache-root"
        (root / "cache").mkdir(parents=True)
        (root / "cache" / "x.txt").write_text("m", encoding="utf-8")
        (root / "outside.txt").write_text("s", encoding="utf-8")
        outside = Path(outer) / "outside-area"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        (root / "link").symlink_to(outside, target_is_directory=True)

        def reference(rel):
            shape = PureWindowsPath(rel)
            if shape.drive or shape.root or ".." in shape.parts:
                return "raise"
            for cand in (root / rel, root / Path(rel).name):
                resolved = Path(cand).resolve()
                if not resolved.is_relative_to(root):
                    return "raise"
                if resolved.is_file():
                    return "file"
            return "none"

        segments = ["cache", "link", "x.txt", "outside.txt", "secret.txt",
                    "..", ".", "", "C:", "srv", "share"]
        corpus = ["cache/x.txt", "cache/../outside.txt", "link/secret.txt",
                  "..", "C:\\x", "\\\\srv\\share\\x", "cache/../../escape"]
        for _ in range(RUNS):
            corpus.append(rng.choice(["/", "\\"]).join(
                rng.choice(segments)
                for _ in range(rng.randint(0, 4))))
        for rel in corpus:
            expect = reference(rel)
            try:
                got = imports._legacy_media_path(rel, str(root))
                actual = "file" if got is not None else "none"
                if actual == "file" and not Path(got).resolve().is_relative_to(root):
                    bad.append(("escaped cache root", rel, str(got)))
            except ValidationError:
                actual = "raise"
            except Exception as exc:
                bad.append(("unexpected exception", rel, type(exc).__name__))
                continue
            if actual != expect:
                bad.append((f"expected {expect}", rel, f"got {actual}"))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# --------------------------------------------------------- 3. accounts.json_body

def prop_json_body(rng):
    """Any byte string body is a dict, a 400, or a 413 — never another raise."""
    class FakeRequest:
        def __init__(self, body):
            self.body = body

    corpus = [
        b"", b"\x00", b'{"a":"\\ud800"}', b'{"a":"\xff"}', b"null", b"[1]",
        b"x" * (MAX_BODY_BYTES + 1), b'{"deep":' + b"[" * 100000,
    ] + [bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 200)))
         for _ in range(RUNS // 2)]
    corpus += [json.dumps(rand_json(rng)).encode("utf-8") for _ in range(RUNS // 2)]
    bad = []
    for body in corpus:
        try:
            out = json_body(FakeRequest(body))
            if not isinstance(out, dict):
                bad.append(("not a dict", body[:40]))
        except ApiError as exc:
            if exc.response.status_code not in (400, 413):
                bad.append(("unexpected status", body[:40],
                            exc.response.status_code))
        except Exception as exc:
            bad.append(("unexpected exception", body[:40], type(exc).__name__))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# ------------------------------------------- 4. admission and worker agreement

def prop_upload_parse(rng):
    """Admission never 500s: for any byte string, decoding plus the engine's
    shape gate raise only ValueError, RecursionError, or ValidationError."""
    corpus = [rand_upload(rng) for _ in range(RUNS)] + [
        b'{"links":{"u":{"url":"x"}}}', b'{"links":{"u":"scalar"}}',
        b'{"links":{},"prompts":[1]}', b'[{"a":1}]', b'[{"url":"u"}]', b"[1]",
        b"{}", b'[' * 100000, '[{"a":1}]'.encode("utf-16"),
    ]
    bad = []
    for body in corpus:
        try:
            data = json.load(io.BytesIO(body))
        except (ValueError, RecursionError):
            continue  # the view's decode gate answers 400
        except Exception as exc:
            bad.append(("decode gate raised", body[:40], type(exc).__name__))
            continue
        try:
            fmt, args = imports.parse_upload(data)
        except ValidationError:
            continue  # api_guard maps the engine's rejection to a 400
        except Exception as exc:
            bad.append(("shape gate raised", body[:40], type(exc).__name__))
            continue
        if fmt == "tiktok" and args["items"] is not data:
            bad.append(("tiktok items not the payload", body[:40]))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


def prop_admission_worker_agreement(rng):
    """Whatever admission accepts, the worker parses to the same payload."""
    data_dir = Path(os.environ["CLIPSHELF_DATA_DIR"])
    staging = data_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    corpus = [rand_upload(rng) for _ in range(RUNS)] + [
        '[{"url":"https://example.com/x"}]'.encode("utf-16"),
        b'\xef\xbb\xbf[{"url":"https://example.com/x"}]',
        b'{"links":{"u":{"url":"x"}}}', b'[{"url":"https://example.com"}]',
    ]
    real_import_items = imports.import_items
    from django.contrib.auth import get_user_model
    fake_user = get_user_model()()  # never saved; the stubbed import ignores it
    captured = []
    imports.import_items = lambda **kw: captured.append(kw) or {"counts": {}}
    bad = []
    try:
        for i, body in enumerate(corpus):
            try:
                data = json.load(io.BytesIO(body))
            except (ValueError, RecursionError):
                continue  # admission refuses; the worker never sees it
            try:
                fmt, args = imports.parse_upload(data)
            except ValidationError:
                continue
            rel = f"staging/prop-{os.getpid()}-{i}.json"
            (data_dir / rel).write_bytes(body)
            record = models.ImportRecord(manifest={"staging": rel})
            record.user = fake_user
            captured.clear()
            try:
                imports.run_staged_import(record)
            except Exception as exc:
                bad.append(("worker diverged from admission", fmt,
                            body[:40], type(exc).__name__))
                continue
            if not captured:
                bad.append(("worker never parsed", fmt, body[:40]))
                continue
            if captured[0]["items"] != args["items"]:
                bad.append(("worker payload differs", body[:40]))
    finally:
        imports.import_items = real_import_items
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# ------------------------------------------------- 5. network._assert_public_ip

def _ref_refuses(text):
    """Reference refusal: unwrap every embedded-IPv4 form ipaddress knows."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True
    check = ip
    if isinstance(ip, ipaddress.IPv6Address):
        emb = ip.ipv4_mapped or ip.sixtofour
        if emb is None and int(ip) >> 32 == 0x64FF9B << 64:  # NAT64 64:ff9b::/96
            emb = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if emb is not None:
            check = emb
    return not (ip.is_global and check.is_global)


def prop_public_ip(rng):
    """_assert_public_ip agrees with the ipaddress reference for embedded
    IPv4 (mapped, 6to4, NAT64) and raises only NetworkError."""
    def v4():
        return tuple(rng.randint(0, 255) for _ in range(4))

    corpus = [
        "64:ff9b::7f00:1", "64:ff9b::192.0.2.1", "::ffff:127.0.0.1",
        "2002:7f00:1::", "127.0.0.1", "8.8.8.8", "2606:4700::1111",
        "::1", "169.254.1.1", "10.0.0.7",
    ]
    for _ in range(RUNS):
        a, b, c, d = v4()
        kind = rng.random()
        if kind < 0.25:
            corpus.append(".".join(map(str, (a, b, c, d))))
        elif kind < 0.45:
            corpus.append(f"::ffff:{a}.{b}.{c}.{d}")
        elif kind < 0.6:
            corpus.append(f"2002:{a:02x}{b:02x}:{c:02x}{d:02x}::")
        elif kind < 0.8:
            corpus.append(rng.choice((
                f"64:ff9b::{a:02x}{b:02x}:{c:02x}{d:02x}",
                f"64:ff9b::{a}.{b}.{c}.{d}",
            )))
        elif kind < 0.9:
            corpus.append(":".join(
                f"{rng.randint(0, 0xffff):x}" for _ in range(rng.randint(1, 8))))
        else:
            corpus.append("".join(rng.choice("0123456789abcdef:. ")
                                  for _ in range(rng.randint(0, 20))))
    bad = []
    for text in corpus:
        try:
            network._assert_public_ip(text)
            refused = False
        except network.NetworkError:
            refused = True
        except Exception as exc:
            bad.append(("unexpected exception", text, type(exc).__name__))
            continue
        if refused != _ref_refuses(text):
            bad.append(("disagrees with reference", text,
                        "refused" if refused else "allowed"))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# ----------------------------------------------------- 6. interpretation._validated

def prop_validated(rng):
    """_validated raises only ValueError, and every kept category label is
    short and visible (no combining-only strings, no blank letters)."""
    def shape(categories):
        return {"summary": "s", "repos": [], "links": [],
                "categories": categories, "prompts": [], "installs": [],
                "warnings": []}

    corpus = [
        shape(["ok"]), shape(["\u0301\u0302"]), shape(["\u3164\u3164"]),
        shape(["cafe\u0301"]), shape(["a" * 41]), shape(["\t"]),
        shape(["\ud83d\ude00 cat"]), shape(rand_json(rng, 1)),
        "not a dict", [], None, 5,
        shape(["\u200b", " \u2800 ", "ok"]),
    ]
    for _ in range(RUNS):
        raw = shape(rand_json(rng, 1))
        if not isinstance(raw["categories"], list):
            raw["categories"] = []
        corpus.append(raw)
    bad = []
    for raw in corpus:
        try:
            out = interpretation._validated(raw, "https://example.com/s", [], [])
        except ValueError:
            continue
        except Exception as exc:
            bad.append(("non-ValueError escape", type(exc).__name__,
                        str(exc)[:60]))
            continue
        for label in out["categories"]:
            if not visible_label(label):
                bad.append(("invisible label kept", repr(label)[:60]))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# --------------------------------------------------------- 7. network.validate_url

class _FakeResolver:
    """socket-module stand-in: canned DNS so no property run touches the net."""

    AF_INET = socket.AF_INET
    SOCK_STREAM = socket.SOCK_STREAM
    gaierror = socket.gaierror

    def __init__(self, address="93.184.216.34"):
        self.address = address

    def getaddrinfo(self, host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (self.address, port))]


def prop_validate_url(rng):
    """validate_url returns an ASCII request_path (http.client can send it),
    or raises NetworkError — never another exception."""
    parts = ["a", "b", "é", "ü", "漢", "%C3%A9", " ", "~", "x"]
    queries = ["", "q=é", "q=%C3%A9", "a=1&b=2", "q=漢"]
    corpus = [
        "https://example.com/héllo", "https://example.com/p?q=é",
        "https://example.com/%C3%A9", "http://example.com/spa ce",
        "https://example.com/x#frag", "ftp://example.com/",
        "https://User:pw@example.com/", "https://example.com:8443/",
        "https://[::1]/", "https://", "https://ex amplex.com/",
    ]
    for _ in range(RUNS):
        corpus.append("https://example.com/" + "".join(
            rng.choice(parts) for _ in range(rng.randint(0, 6)))
            + "?" + rng.choice(queries))
    fake = _FakeResolver()
    original, network.socket = network.socket, fake
    bad = []
    try:
        for url in corpus:
            try:
                scheme, host, port, path, infos = network.validate_url(url)
            except network.NetworkError:
                continue
            except Exception as exc:
                bad.append(("unexpected exception", url, type(exc).__name__))
                continue
            try:
                path.encode("ascii")
            except UnicodeEncodeError:
                bad.append(("non-ASCII request path", url, path))
                continue
            if not path.startswith("/"):
                bad.append(("path has no leading slash", url, path))
    finally:
        network.socket = original
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# --------------------------------------- 8. follows.validate_profile_url

_PROFILE_SHAPE = re.compile(r"^/@[^/@]+$")


def _ref_profile(url):
    """Independent restatement of the spec: tiktok.com host only, and the
    normed path exactly /@handle; the canonical form or None."""
    normalized = lib.norm(url) if isinstance(url, str) else None
    if not normalized or not lib.is_tiktok(normalized):
        return None
    path = urlsplit(normalized).path
    if not _PROFILE_SHAPE.match(path):
        return None
    return f"https://www.tiktok.com{path.lower()}"


def prop_profile_url(rng):
    """validate_profile_url agrees with the spec restatement: only ValueError,
    and any returned form is the canonical /@handle URL for that input."""
    corpus = [
        "https://www.tiktok.com/@creator", "https://tiktok.com/@User?utm=x",
        "https://m.tiktok.com/@USER", "https://www.tiktok.com/@user/",
        "https://www.tiktok.com/@", "https://www.tiktok.com/@/",
        "https://www.tiktok.com/@", "https://www.tiktok.com/discover/@foo",
        "https://www.tiktok.com/@user/video/123", "https://vm.tiktok.com/@x",
        "https://evil-tiktok.com/@user", "https://tiktok.com.evil.com/@user",
        "https://example.com/@user", "gopher://tiktok.com/@user",
        "https://User:pw@tiktok.com/@user", "not a url", "", None, 7,
    ]
    hosts = ["www.tiktok.com", "tiktok.com", "m.tiktok.com",
             "evil-tiktok.com", "example.com", ""]
    paths = ["/@user", "/@USER", "/@", "/@@", "/@/", "/@user/video/1",
             "/discover/@foo", "/@foo/", "/@a.b_c-d", "/video/123",
             "/", "", "/@héllo"]
    alphabet = "abcABC019:/?#[]@!$&'()*+,;=-._~%é\t <>\"\\|"
    for url in corpus + [
        *(f"https://{rng.choice(hosts)}{rng.choice(paths)}"
          for _ in range(RUNS)),
        *("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
          for _ in range(RUNS)),
    ]:
        expect = _ref_profile(url)
        try:
            got = follows.validate_profile_url(url)
        except ValueError:
            got = None
        except Exception as exc:
            raise AssertionError(f"non-ValueError for {url!r}: {exc!r}")
        if got != expect:
            raise AssertionError(f"expected {expect!r} for {url!r}, got {got!r}")


# ------------------------------------------------- 8. acquisition.list_profile_posts

def prop_list_profile_posts(rng):
    """Any extractor stdout raises only ListingError, or yields posts whose
    ids are at most 64 characters and whose URLs are http(s)."""
    url = "https://www.tiktok.com/@creator"
    corpus = [
        b'{"entries": [{"id": "7101",'
        b' "url": "https://www.tiktok.com/@c/video/7101"}]}',
        b'{"entries": []}', b'{"entries": {}}', b'[]', b'null', b'{}',
        b'not json', b'', b'\xff\xfe{"entries": []}',
        b'{"entries": [{"id": "' + b"9" * 300 + b'", "url": "https://x/y"}]}',
        b'{"entries": [{"id": 7102, "url": 7102}]}',
        b'{"entries": [{"id": "7103", "url": "ftp://x/y"},'
        b' {"id": "7104", "url": "https://x"}]}',
        b'{"entries": ["nope", null, 3]}',
        b'{"entries": [{"id": "7105", "webpage_url": "https://x/y"}]}',
    ]
    for _ in range(RUNS):
        corpus.append(json.dumps(rand_json(rng)).encode("utf-8"))
    holder = {}
    saved = (acquisition._run_listing, network.extractor_egress)

    def fake_run(argv):
        return 0, holder["stdout"], ""

    def fake_egress():
        return contextlib.nullcontext("http://127.0.0.1:0")

    bad = []
    try:
        acquisition._run_listing = fake_run
        network.extractor_egress = fake_egress
        for stdout in corpus:
            holder["stdout"] = stdout
            try:
                posts = acquisition.list_profile_posts(url)
            except acquisition.ListingError:
                continue
            except Exception as exc:
                bad.append(("unexpected exception", stdout[:60],
                            type(exc).__name__))
                continue
            for post in posts:
                if not (isinstance(post["id"], str) and post["id"]
                        and len(post["id"]) <= 64):
                    bad.append(("bad id", stdout[:60], repr(post["id"])[:60]))
                if not post["url"].startswith(("http://", "https://")):
                    bad.append(("non-http url", stdout[:60],
                                repr(post["url"])[:60]))
    finally:
        acquisition._run_listing, network.extractor_egress = saved
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# ------------------------------------------------------- 9. /api/feed queries

ISO_OK = [
    "2026-01-15T10:30:00+00:00", "2026-01-15T10:30:00Z",
    "2026-01-15T10:30:00.123456+02:00", "2026-06-01T00:00:00-08:00",
]
SINCE_BAD = [
    "2026-02-30T00:00:00+00:00",   # the shipped 500 counterexample
    "0000-00-00T00:00:00+00:00", "2026-13-45T09:99:99+00:00",
    "not-a-date", "", "  ", "2026-01-15", "10:30:00",
    "2026-01-15T10:30:00",                          # naive: no UTC offset
    "x" * 300, "２０２６-01-15T00:00:00+00:00",
    "2026-01-15T10:30:00+00:00' --", "2026-01-15T10:30:00+99:99",
]
LIMIT_SAMPLES = ["", "abc", "0", "-1", "100", "501",
                 "99999999999999999999999", " 42 ", "+7", "1_0", "1.5",
                 "١٢", "１２", "None", "500e0"]
TAG_SAMPLES = ["", " music ", "MUSIC", "café", "<script>", "\u3164",
               "a" * 300, "music\u0301", "x; drop table", "♪"]

_feed_state = {}


def _feed_client():
    """One authenticated client on a throwaway in-memory test database."""
    if "client" not in _feed_state:
        test_utils.setup_test_environment()
        db_runner = test_runner.DiscoverRunner(interactive=False, verbosity=0)
        db_runner.setup_databases()
        email = "properties@example.com"
        user = get_user_model().objects.create_user(
            username=email, email=email, password="property-pass-1234")
        EmailAddress.objects.create(user=user, email=email,
                                    verified=True, primary=True)
        services.personal_collection(user)
        client = Client()
        client.force_login(user)
        _feed_state["client"] = client
    return _feed_state["client"]


def rand_since(rng):
    """ISO-shaped values that are often calendar-invalid, plus pure garbage."""
    if rng.random() < 0.5:
        return rng.choice(ISO_OK + SINCE_BAD)
    return (f"2026-{rng.randint(0, 99):02d}-{rng.randint(0, 99):02d}"
            f"T{rng.randint(0, 99):02d}:{rng.randint(0, 99):02d}"
            f":{rng.randint(0, 99):02d}"
            f"+{rng.choice(['00', '05', '13', '99'])}"
            f":{rng.choice(['00', '30', '99'])}")


def prop_feed(rng):
    """/api/feed answers 200 or 400 for any query values — never 500."""
    client = _feed_client()
    corpus = [
        {"since": "2026-02-30T00:00:00+00:00"},  # shipped 500 counterexample
        {}, {}, {"since": "2026-01-15T10:30:00+00:00"},
        {"since": "2026-01-15T10:30:00+00:00", "limit": "10",
         "tag": " music "},
    ]
    for _ in range(RUNS):
        query = {}
        if rng.random() < 0.9:
            query["since"] = rand_since(rng)
        if rng.random() < 0.7:
            query["limit"] = rng.choice(
                LIMIT_SAMPLES + [str(rng.randint(-5, 700))])
        if rng.random() < 0.7:
            query["tag"] = rng.choice(TAG_SAMPLES)
        corpus.append(query)
    bad = []
    for query in corpus:
        try:
            response = client.get("/api/feed", query)
        except Exception as exc:
            # DEBUG mode re-raises view crashes; a crash is never a 4xx.
            bad.append(("view raised", query, type(exc).__name__))
            continue
        if response.status_code not in (200, 400):
            bad.append(("status", response.status_code, query,
                        response.content[:120]))
            continue
        if response.status_code == 200:
            payload = json.loads(response.content)
            if not (isinstance(payload.get("entries"), list)
                    and isinstance(payload.get("count"), int)):
                bad.append(("body shape", query))
    assert not bad, f"({len(bad)} violations) {bad[:3]}"


# --------------------------------------------------------------------- harness

PROPERTIES = [
    ("norm", prop_norm),
    ("legacy_media_path", prop_legacy_media_path),
    ("json_body", prop_json_body),
    ("upload_parse", prop_upload_parse),
    ("admission_worker_agreement", prop_admission_worker_agreement),
    ("public_ip", prop_public_ip),
    ("validated", prop_validated),
    ("validate_url", prop_validate_url),
    ("profile_url", prop_profile_url),
    ("list_profile_posts", prop_list_profile_posts),
    ("feed", prop_feed),
]


def main():
    print(f"seed {SEED}, {RUNS} runs per property")
    failures = []
    for name, fn in PROPERTIES:
        rng = random.Random(SEED)
        try:
            fn(rng)
            print(f"ok   {name}")
        except AssertionError as exc:
            failures.append(f"{name} (seed {SEED}): {exc}")
            print(f"FAIL {name} (seed {SEED}): {exc}")
    if failures:
        print(f"\n{len(failures)} property(ies) failed; "
              f"pin each counterexample as an explicit example above.")
        return 1
    print(f"\nall {len(PROPERTIES)} properties hold (seed {SEED}, "
          f"{RUNS} runs each)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
