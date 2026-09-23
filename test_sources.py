"""Self-check: python test_sources.py — SSRF transport/redirect pinning plus
malformed media and output bounds. Offline: sockets and endpoints are faked."""
import base64
import gzip
import json
import io
import socket
import tempfile
import os
import unittest
from unittest import mock

import clipshelf.acquisition as acq
import clipshelf.interpretation as interp
import clipshelf.network as net
from PIL import Image

PUBLIC = "93.184.216.34"


class FakeSock:
    def __init__(self, fake):
        self.fake, self.file = fake, None

    def connect(self, sa):
        self.fake.connected.append(sa[0])
        head = f"HTTP/1.1 {self.fake.status} X\r\nContent-Type: {self.fake.ctype}\r\n"
        if self.fake.encoding:
            head += f"Content-Encoding: {self.fake.encoding}\r\n"
        for k, v in self.fake.extra:
            head += f"{k}: {v}\r\n"
        head += f"Content-Length: {len(self.fake.body)}\r\n\r\n"
        self.file = io.BytesIO(head.encode() + self.fake.body)

    def getsockopt(self, level, opt):
        return socket.SOCK_STREAM

    def makefile(self, *a, **k):
        return self.file

    def sendall(self, data):
        self.fake.sent.append(data)

    def settimeout(self, t):
        pass

    def close(self):
        pass


class FakeNet:
    """socket module stand-in: canned DNS + one canned HTTP response per connect."""

    AF_INET = socket.AF_INET
    SOCK_STREAM = socket.SOCK_STREAM
    gaierror = socket.gaierror

    def __init__(self, addresses, responses):
        self.addresses, self.responses = addresses, list(responses)
        self.connected, self.queried, self.sent = [], [], []

    def getaddrinfo(self, host, port, *a, **k):
        self.queried.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in self.addresses.get(host, [])]

    def socket(self, family, type):
        sock = FakeSock(self)
        status, ctype, body, encoding, extra = self.responses.pop(0)
        sock.fake.status, sock.fake.ctype = status, ctype
        sock.fake.body, sock.fake.encoding, sock.fake.extra = body, encoding, extra
        return sock


def patched(addresses, responses):
    fake = FakeNet(addresses, responses)
    original = net.socket
    net.socket = fake
    return fake, original


def http(status=200, ctype="text/html", body=b"ok", encoding=None, extra=()):
    return (status, ctype, body, encoding, list(extra))


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_ssrf_pinned_transport():
    # private/loopback literals and private DNS records are refused outright
    for bad in ("http://127.0.0.1/x", "http://10.0.0.1/x", "http://[::1]/x",
                "http://169.254.169.254/meta", "http://0.0.0.0/x"):
        fake, original = patched({}, [])
        try:
            try:
                net.fetch_public(bad)
                raise AssertionError(f"accepted {bad}")
            except net.NetworkError:
                pass
            assert fake.connected == [], bad
        finally:
            net.socket = original

    # credentials, non-standard ports, odd schemes never open a socket
    for bad in ("http://user:pass@example.com/x", "http://example.com:8080/x",
                "ftp://example.com/x", "http:///nohost"):
        fake, original = patched({"example.com": [PUBLIC]}, [])
        try:
            try:
                net.fetch_public(bad)
                raise AssertionError(f"accepted {bad}")
            except net.NetworkError:
                pass
            assert fake.connected == [], bad
        finally:
            net.socket = original

    # pinning: the connection goes to the validated IP, not the hostname
    fake, original = patched({"example.com": [PUBLIC]}, [http(body=b"hello")])
    try:
        out = net.fetch_public("http://example.com/a")
        assert out["url"] == "http://example.com/a" and out["body"] == b"hello"
        assert out["content_type"] == "text/html"
        assert fake.connected == [PUBLIC], fake.connected
        assert b"Host: example.com" in fake.sent[0]
    finally:
        net.socket = original

    # mixed public+private DNS (rebinding shape) fails closed
    fake, original = patched({"example.com": [PUBLIC, "10.0.0.7"]}, [])
    try:
        try:
            net.fetch_public("http://example.com/")
            raise AssertionError("mixed records accepted")
        except net.NetworkError as exc:
            assert "non-public" in str(exc)
        assert fake.connected == []
    finally:
        net.socket = original


def test_redirects_and_bounds():
    # redirect into a private net is re-validated and refused
    fake, original = patched(
        {"example.com": [PUBLIC], "internal.example": ["192.168.0.1"]},
        [http(302, extra=[("Location", "http://internal.example/landing")])])
    try:
        try:
            net.fetch_public("http://example.com/go")
            raise AssertionError("redirect to private accepted")
        except net.NetworkError as exc:
            assert "non-public" in str(exc)
        assert fake.connected == [PUBLIC]  # hop 1 public; hop 2 refused pre-connect
    finally:
        net.socket = original

    # public redirect chain resolves and returns the FINAL url
    fake, original = patched(
        {"example.com": [PUBLIC], "www.example.com": [PUBLIC]},
        [http(302, extra=[("Location", "http://www.example.com/final")]),
         http(body=b"landed")])
    try:
        out = net.fetch_public("http://example.com/go")
        assert out["url"] == "http://www.example.com/final"
        assert out["body"] == b"landed"
        assert len(fake.connected) == 2
    finally:
        net.socket = original

    # decompression bomb dies at the byte bound
    bomb = gzip.compress(b"\0" * 2_000_000)
    fake, original = patched({"example.com": [PUBLIC]},
                             [http(body=bomb, encoding="gzip")])
    try:
        try:
            net.fetch_public("http://example.com/", max_bytes=4096)
            raise AssertionError("bomb accepted")
        except net.NetworkError as exc:
            assert "bound" in str(exc)
    finally:
        net.socket = original

    # gzip body decodes normally within bounds; error statuses refuse
    page = b"<html><title>t</title></html>"
    fake, original = patched({"example.com": [PUBLIC]},
                             [http(body=gzip.compress(page), encoding="gzip"),
                              http(404, body=b"gone")])
    try:
        out = net.fetch_public("http://example.com/", max_bytes=1 << 20)
        assert out["body"] == page
        try:
            net.fetch_public("http://example.com/missing")
            raise AssertionError("404 accepted")
        except net.NetworkError as exc:
            assert "404" in str(exc)
    finally:
        net.socket = original


def test_import_media_bounds(tmp=None):
    tmp = tmp or tempfile.mkdtemp(prefix="cs-src-")
    good = base64.b64encode(png_bytes()).decode()

    # payload-level violations raise
    for bad_items in ("nope", [{"no": "url"}], [{"images": []}]):
        try:
            acq.import_export(bad_items, tmp)
            raise AssertionError(f"accepted {bad_items!r}")
        except acq.AcquisitionError:
            pass

    # valid embedded slide imports, decodes, and completes
    out = acq.import_export(
        [{"url": "https://www.tiktok.com/@u/photo/1", "resolvedUrl":
          "https://www.tiktok.com/@u/photo/1", "desc": "d",
          "images": [{"url": "https://cdn.example/s.jpg", "b64": good}]}], tmp)
    assert len(out) == 1
    src = out[0]
    assert src["acquisition"] == "complete" and src["metadata"]["media"] == "image"
    assert src["metadata"]["tier"] == "browser-export"
    assert len(src["assets"]) == 1 and src["assets"][0]["kind"] == "image"
    with Image.open(src["assets"][0]["path"]) as im:
        assert im.size == (4, 4)
    assert os.path.realpath(src["assets"][0]["path"]).startswith(os.path.realpath(tmp))

    # malformed slide bytes: honest per-item failure, no fabricated success
    junk = base64.b64encode(b"definitely not an image").decode()
    out = acq.import_export([{"url": "https://www.tiktok.com/@u/photo/2",
                              "images": [{"b64": junk}]}], tmp)
    assert out[0]["acquisition"] in ("partial", "blocked")
    assert any("failed validation" in w for w in out[0]["warnings"])

    # oversized embedded media is rejected with an honest warning
    huge = base64.b64encode(b"\0" * (acq.IMPORT_MEDIA_MAX_BYTES + 16)).decode()
    out = acq.import_export([{"url": "https://www.tiktok.com/@u/photo/3",
                              "images": [{"b64": huge}]}], tmp)
    assert out[0]["acquisition"] == "error"
    assert any("rejected" in w for w in out[0]["warnings"])

    # browser export errors stay saved and visibly blocked
    out = acq.import_export([{"url": "https://www.tiktok.com/@u/video/4",
                              "error": "statusCode 10216"}], tmp)
    assert out[0]["acquisition"] == "blocked"
    assert any("10216" in w for w in out[0]["warnings"])

    # video mirrors: ftyp-verified bytes from pinned public fetch
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 64
    real_fetch = net.fetch_public
    net.fetch_public = lambda url, **k: {"url": url, "body": mp4,
                                         "content_type": "video/mp4"}
    real_validate = acq.validate_video_file
    acq.validate_video_file = lambda path: {"duration": 1.0}
    try:
        out = acq.import_export([{"url": "https://www.tiktok.com/@u/video/5",
                                  "video": {"urls": ["https://cdn.example/v.mp4"]}}], tmp)
        assert out[0]["acquisition"] == "complete", out[0]["warnings"]
        assert out[0]["metadata"]["media"] == "video"
        with open(out[0]["assets"][0]["path"], "rb") as fh:
            assert fh.read(8)[4:8] == b"ftyp"
    finally:
        net.fetch_public = real_fetch
        acq.validate_video_file = real_validate
    print("import bounds ok")


def test_interpretation_bounds():
    categories = ["github", "prompts"]
    source = {"url": "https://example.com/page", "title": "T", "desc": "D",
              "text": "body", "links": [], "assets": [], "metadata": {}}
    cfg = {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": None}

    # config failures never invent findings
    for bad in ({}, {"base_url": "x", "model": "m"}, {"base_url": "http://h/x"},
                {"base_url": "http://h/x", "model": " "}):
        try:
            interp.interpret(source, bad, categories)
            raise AssertionError(f"config accepted: {bad}")
        except interp.ConfigurationError:
            pass
    report = interp.check_connection({"model": "m"})
    assert report["ok"] is False

    # findings shape/size/URL/category validation
    good_raw = '{"summary": "s", "repos": ["owner/repo"], "prompts": [], ' \
               '"links": ["https://github.com/a/b"], "categories": ["GITHUB"], ' \
               '"installs": [], "warnings": []}'
    findings = interp._validated(interp._parse_json_content(good_raw),
                                 source["url"], categories, [])
    assert findings["source"] == source["url"]
    assert findings["repos"] == ["https://github.com/owner/repo"]
    assert findings["categories"] == ["github"]

    # oversize summary, garbage links, wrong types -> malformed (retry path)
    for bad_raw in (
            '{"summary": "' + "x" * 5000 + '", "repos": [], "prompts": [], '
            '"links": [], "categories": [], "installs": [], "warnings": []}',
            '{"summary": "s", "repos": [], "prompts": [42], "links": [], '
            '"categories": [], "installs": [], "warnings": []}',
            '{"summary": "s", "repos": [], "prompts": [], "links": ["not a url"], '
            '"categories": [], "installs": [], "warnings": []}',
            '{"summary": "s", "repos": [], "prompts": [], "links": [], '
            '"installs": [], "warnings": []}'):
        try:
            interp._validated(interp._parse_json_content(bad_raw),
                              source["url"], categories, [])
            raise AssertionError(f"findings accepted: {bad_raw[:60]}")
        except (ValueError, KeyError):
            pass

    # unknown categories are dropped with a warning, not fatal
    warnings = []
    mixed = interp._validated(interp._parse_json_content(
        '{"summary": "s", "repos": [], "prompts": [], "links": [], '
        '"categories": ["github", "bogus"], "installs": [], "warnings": []}'),
        source["url"], categories, warnings)
    assert mixed["categories"] == ["github"]
    assert any("bogus" in w for w in warnings)

    # interpret: one malformed retry then preserve the failure (2 POSTs max)
    real_post = interp.post
    calls = []

    def fake_post(base, key, payload, timeout):
        calls.append(payload)
        content = '{"summary": "' + "x" * 5000 + '", "repos": [], "prompts": [], ' \
                  '"links": [], "categories": [], "installs": [], "warnings": []}'
        return {"choices": [{"message": {"content": content}}]}

    interp.post = fake_post
    try:
        try:
            interp.interpret(source, cfg, categories)
        except interp.InterpretationError as exc:
            assert "retry" in str(exc)
        assert len(calls) == 2, len(calls)  # exactly one malformed retry
    finally:
        interp.post = real_post

    # happy path: source identity injected, unverified URLs flagged honestly
    def ok_post(base, key, payload, timeout):
        return {"choices": [{"message": {"content": good_raw}}]}

    real_fetch = net.fetch_public
    net.fetch_public = lambda url, **k: (_ for _ in ()).throw(net.NetworkError("down"))
    interp.post = ok_post
    try:
        findings = interp.interpret(source, cfg, categories)
        assert findings["source"] == source["url"]
        assert any("unverified: https://github.com/a/b" in w
                   for w in findings["warnings"])
    finally:
        interp.post = real_post
        net.fetch_public = real_fetch

    # key redaction: check_connection never leaks the api key
    def leak_post(base, key, payload, timeout):
        raise Exception(f"connect failed for key {key} at host")

        interp.post = leak_post
    try:
        report = interp.check_connection({"base_url": "http://h/x", "model": "m",
                                          "api_key": "sekret"})
        assert report["ok"] is False and "sekret" not in report["message"]
    finally:
        interp.post = real_post

    # the check gives reasoning models output headroom (same ceiling as interpret)
    def spy_post(base, key, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": '{"ok": true, "color": "red"}'},
                             "finish_reason": "stop"}]}

    interp.post = spy_post
    try:
        report = interp.check_connection({"base_url": "http://h/x", "model": "m"})
        assert report["ok"] is True
        assert calls[-1]["max_tokens"] == interp.OUTPUT_TOKENS_MAX
    finally:
        interp.post = real_post

    # a length-truncated empty answer reports the ceiling, not raw response soup
    def long_post(base, key, payload, timeout):
        return {"choices": [{"message": {"content": "", "reasoning_content": "thinking"},
                             "finish_reason": "length"}]}

    interp.post = long_post
    try:
        report = interp.check_connection({"base_url": "http://h/x", "model": "m"})
        assert report["ok"] is False and "output ceiling" in report["message"]
        assert "thinking" not in report["message"]
    finally:
        interp.post = real_post
    print("interpretation bounds ok")


def test_screening_policy():
    """Optional fail-open screening: block severe steering, withhold the rest.

    Returns a FunctionTestCase so `clipshelf.py test test_sources.test_screening_policy`
    and the module self-runner both execute exactly one pass."""
    def check():
        categories = ["github", "prompts"]
        source = {"url": "https://example.com/page", "title": "T", "desc": "D",
                  "text": "body", "links": [], "assets": [], "metadata": {}}
        cfg = {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": None}
        good_raw = '{"summary": "s", "repos": ["owner/repo"], "prompts": [], ' \
                   '"links": [], "categories": ["github"], "installs": ["pip install foo"], ' \
                   '"warnings": []}'

        def ok_post(base, key, payload, timeout):
            return {"choices": [{"message": {"content": good_raw}}]}

        calls = []

        def spy_post(base, key, payload, timeout):
            calls.append(payload)
            return {"choices": [{"message": {"content": good_raw}}]}

        def screen(steer, sev=0.0):
            return lambda state, api_key=None: {"text_steer": steer,
                                                "meta_steer": 0.0, "severity": sev}

        # screening unavailable: plain findings, screen_material never consulted
        with mock.patch.object(interp.judgment, "available", return_value=False), \
             mock.patch.object(interp.judgment, "screen_material",
                               side_effect=AssertionError("must not screen")), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert findings["installs"] == ["pip install foo"]
        assert not any("screening" in w for w in findings["warnings"])

        # high steer + severe: deterministic block before any endpoint call
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material",
                               screen(0.98, sev=2.0)), \
             mock.patch.object(interp, "post", spy_post):
            try:
                interp.interpret(source, cfg, categories)
                raise AssertionError("severe interpreter-directed content accepted")
            except interp.GuardrailBlocked as exc:
                assert isinstance(exc, interp.InterpretationError)
        assert calls == []

        # high steer, low severity: endpoint still called, results withheld
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material",
                               screen(0.90, sev=1.0)), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == [] and findings["installs"] == []
        assert any("repos and installs withheld" in w for w in findings["warnings"])

        # mid steer: flagged, but findings pass through intact
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material", screen(0.50)), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert any("suspicious content flagged" in w for w in findings["warnings"])

        # screening failure: fail-open with a visible warning
        def boom(state, api_key=None):
            raise interp.judgment.JudgmentError("no api key")

        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material", boom), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert any("screening unavailable" in w for w in findings["warnings"])
        print("screening policy ok")
    return unittest.FunctionTestCase(check)


def test_list_models():
    """The admin picker's source of truth: what the endpoint says it has."""
    calls = []

    class Resp:
        def __init__(self, body): self.body = body
        def read(self, n): return self.body[:n]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization")))
        return Resp(json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"},
                                         {"id": 7}, {"id": " "}, {}, None, "junk"]}).encode())

    real_open = interp.urllib.request.urlopen
    interp.urllib.request.urlopen = fake_open
    try:
        # sorted, deduped, non-string ids dropped; trailing slash not doubled
        assert interp.list_models({"base_url": "http://h/v1/", "api_key": "sekret"}) == ["a", "b"]
        assert calls[0] == ("http://h/v1/models", "Bearer sekret")
        # a local endpoint without a key sends no Authorization header
        interp.list_models({"base_url": "http://h/v1"})
        assert calls[1][1] is None

        def denied(req, timeout=None):
            raise interp.urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b""))

        interp.urllib.request.urlopen = denied
        try:
            interp.list_models({"base_url": "http://h/v1", "api_key": "sekret"})
            raise AssertionError("401 accepted")
        except interp.ConfigurationError as exc:
            assert "credentials" in str(exc) and "sekret" not in str(exc)

        def unreachable(req, timeout=None):
            raise interp.urllib.error.URLError("refused by sekret")

        interp.urllib.request.urlopen = unreachable
        try:
            interp.list_models({"base_url": "http://h/v1", "api_key": "sekret"})
            raise AssertionError("unreachable accepted")
        except interp.ConfigurationError as exc:
            assert "sekret" not in str(exc)

        # a server that answers but not with a model list is a config error,
        # never an empty picker that looks like "no models installed"
        interp.urllib.request.urlopen = lambda req, timeout=None: Resp(b"<html>nope</html>")
        try:
            interp.list_models({"base_url": "http://h/v1"})
            raise AssertionError("garbage accepted")
        except interp.ConfigurationError:
            pass
        interp.urllib.request.urlopen = lambda req, timeout=None: Resp(b'{"data":{}}')
        try:
            interp.list_models({"base_url": "http://h/v1"})
            raise AssertionError("non-list data accepted")
        except interp.ConfigurationError:
            pass
        limit = interp.LLM_RESPONSE_MAX
        interp.LLM_RESPONSE_MAX = 16
        try:
            interp.urllib.request.urlopen = lambda req, timeout=None: Resp(b'{"data":[]}' + b" " * 20)
            try:
                interp.list_models({"base_url": "http://h/v1"})
                raise AssertionError("oversized response accepted")
            except interp.ConfigurationError:
                pass
        finally:
            interp.LLM_RESPONSE_MAX = limit
    finally:
        interp.urllib.request.urlopen = real_open
    print("model listing ok")


def test():
    test_ssrf_pinned_transport()
    print("ssrf/pin ok")
    test_redirects_and_bounds()
    print("redirect/bounds ok")
    test_import_media_bounds()
    test_interpretation_bounds()
    test_screening_policy().debug()
    test_list_models()
    print("ok")


if __name__ == "__main__":
    test()
