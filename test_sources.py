"""Self-check: python test_sources.py — SSRF transport/redirect pinning plus
malformed media and output bounds. Offline: sockets and endpoints are faked."""
import base64
import gzip
import json
import io
import random
import socket
import shutil
import tempfile
import os
import time
import threading
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


def jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (3, 3), (5, 5, 5)).save(buf, "JPEG")
    return buf.getvalue()


class _Resp:
    """urlopen stand-in: bounded read + context manager, for post()-level tests."""

    def __init__(self, body):
        self.body = body

    def read(self, n):
        return self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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


def test_deadline_bounded_reads():
    # a slow trickle must fail at the whole-fetch deadline, not when the last
    # byte finally arrives through a blocking buffered read
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            conn.sendall(b"HTTP/1.1 200 X\r\nContent-Type: application/octet-stream\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\n")
            for gap in (0.75, 1.5):
                time.sleep(gap)
                conn.sendall(b"x")
            time.sleep(0.2)
            conn.close()
        except OSError:
            pass

    real_validate = net.validate_url
    net.validate_url = lambda url: ("http", "media.example", port, "/",
                                    [(socket.AF_INET, ("127.0.0.1", port))])
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    start = time.monotonic()
    try:
        try:
            net.fetch_public("http://media.example/x", timeout=1.0)
            raise AssertionError("trickled body accepted past deadline")
        except net.NetworkError as exc:
            elapsed = time.monotonic() - start
            assert "time bound" in str(exc), str(exc)
            assert elapsed < 2.0, f"slow reader held the fetch {elapsed:.2f}s"
    finally:
        net.validate_url = real_validate
        srv.close()

    # address retries share one budget: two refused dials must not each take
    # the full timeout
    class SlowRefusedSock(FakeSock):
        def settimeout(self, t):
            self.timeout_left = t

        def connect(self, sa):
            self.fake.connected.append(sa[0])
            time.sleep(min(0.7, getattr(self, "timeout_left", 0) or 0.7))
            raise ConnectionRefusedError("refused")

    class RetryNet(FakeNet):
        def socket(self, family, type):
            return SlowRefusedSock(self)

    fake = RetryNet({"media.example": [PUBLIC, "203.0.113.9"]}, [])
    real_socket = net.socket
    net.validate_url = lambda url: ("http", "media.example", 80, "/",
                                    [(socket.AF_INET, (PUBLIC, 80)),
                                     (socket.AF_INET, ("203.0.113.9", 80))])
    net.socket = fake
    start = time.monotonic()
    try:
        try:
            net.fetch_public("http://media.example/x", timeout=1.0)
            raise AssertionError("retry past deadline accepted")
        except net.NetworkError:
            elapsed = time.monotonic() - start
            assert elapsed < 1.2, f"retry budget doubled the fetch: {elapsed:.2f}s"
    finally:
        net.socket = real_socket
        net.validate_url = real_validate


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


def test_acquisition_regressions(tmp):
    # Repeated staged names must retain distinct files and bytes.
    name_dir = os.path.join(tmp, "same-name")
    os.makedirs(name_dir, exist_ok=True)
    payloads = []
    for color in ((10, 200, 30), (30, 10, 200), (200, 30, 10)):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), color).save(buf, "PNG")
        payloads.append(buf.getvalue())
    url = "https://www.tiktok.com/@u/photo/collision"
    items = [{"url": url, "images": [{"b64": base64.b64encode(data).decode()}]}
             for data in payloads]
    sources = acq.import_export(items, name_dir)
    paths = [source["assets"][0]["path"] for source in sources]
    assert len(set(paths)) == len(payloads), paths
    assert all(os.path.splitext(path)[1] == ".png" for path in paths)
    actual = []
    for path in paths:
        with open(path, "rb") as fh:
            actual.append(fh.read())
    assert actual == payloads

    # Validation failure stays partial even after the rejected video is removed.
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 64
    with mock.patch.object(acq, "validate_video_file",
                           side_effect=ValueError("detail")):
        source = acq.import_export(
            [{"url": "https://www.tiktok.com/@u/video/invalid",
              "video": {"b64": base64.b64encode(mp4).decode()}}], tmp)[0]
    assert source["acquisition"] == "partial"
    assert source["assets"] == []
    assert "video failed validation: detail" in source["warnings"]

    # A cover thumbnail is not the acquired TikTok content.
    thumbnail_url = "https://cdn.example/cover.png"

    def fake_fetch(url, **kwargs):
        if url.startswith("https://www.tiktok.com/oembed?"):
            return {"body": json.dumps({
                "title": "Post", "thumbnail_url": thumbnail_url}).encode()}
        if url == thumbnail_url:
            return {"body": payloads[0], "content_type": "image/png"}
        raise AssertionError(f"unexpected fetch: {url}")

    with mock.patch.object(net, "fetch_public", side_effect=fake_fetch), \
         mock.patch.object(net, "extractor_egress", return_value=mock.MagicMock()), \
         mock.patch.object(acq, "_run_extractor", return_value=(1, "no media")):
        source = acq.acquire("https://www.tiktok.com/@u/video/cover", tmp)
    assert source["metadata"]["metadata"] == "oembed"
    assert source["metadata"]["media"] == "image"
    assert source["acquisition"] == "partial"

    # A video without captions remains complete but names the content gap.
    def emit_video(argv):
        flag = "--paths" if "--paths" in argv else "-d"
        outdir = argv[argv.index(flag) + 1]
        with open(os.path.join(outdir, "video.mp4"), "wb") as fh:
            fh.write(mp4)
        return 0, ""

    with mock.patch.object(acq, "_oembed",
                           return_value={"title": "", "author": "",
                                         "thumbnail_url": ""}), \
         mock.patch.object(net, "extractor_egress", return_value=mock.MagicMock()), \
         mock.patch.object(acq, "_run_extractor", side_effect=emit_video), \
         mock.patch.object(acq, "validate_video_file", return_value={"duration": 1.0}):
        source = acq.acquire("https://www.tiktok.com/@u/video/no-captions", tmp)
    assert source["acquisition"] == "complete"
    assert source["metadata"]["captions"] == "none"
    assert any("no captions available" in warning for warning in source["warnings"])

    # Reject excessive image dimensions before decoding pixel data.
    image = mock.MagicMock()
    image.__enter__.return_value = image
    image.size = (acq.IMAGE_MAX_PIXELS + 1, 1)
    image.format = "PNG"
    with mock.patch.object(acq.Image, "open", return_value=image):
        try:
            acq.validate_image(b"image")
        except ValueError as exc:
            assert "exceed bound" in str(exc)
        else:
            raise AssertionError("oversized image accepted")
    image.load.assert_not_called()


def test_interpretation_regressions(tmp):
    config = {"base_url": "http://h/v1", "model": "m"}
    source = {"url": "https://example.com/page", "title": "", "desc": "",
              "text": "", "links": [], "metadata": {}}
    result = {"summary": "s", "repos": [], "prompts": [], "links": [],
              "categories": [], "installs": [], "warnings": []}
    calls = []

    def fake_post(base, key, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": json.dumps(result)}}]}

    # An oversized local image is skipped, not allowed to abort interpretation.
    image_path = os.path.join(tmp, "interpretation-oversize.img")
    with open(image_path, "wb") as fh:
        fh.write(b"too large")
    source["assets"] = [{"kind": "image", "path": image_path}]
    with mock.patch.object(interp, "IMAGE_FILE_MAX", 8), \
         mock.patch.object(interp.judgment, "available", return_value=False), \
         mock.patch.object(interp, "post", side_effect=fake_post):
        findings = interp.interpret(source, config, [])
    assert len(calls) == 1
    assert any("size bound" in warning for warning in findings["warnings"])

    # URL body overflow alone does not mean the endpoint is unverified.
    large_url = "https://example.com/large"
    unavailable_url = "https://example.com/unavailable"

    def fetch(url, **kwargs):
        if url == large_url:
            raise net.NetworkError("response exceeds 65536 byte bound")
        raise net.NetworkError("offline")

    findings = {"repos": [], "links": [large_url, unavailable_url], "warnings": []}
    with mock.patch.object(net, "fetch_public", side_effect=fetch):
        interp._verify_urls(findings, [])
    assert not any(large_url in warning for warning in findings["warnings"])
    assert any(unavailable_url in warning for warning in findings["warnings"])

    # Screening diagnostics survive the model's ten-warning allowance.
    model_warnings = [f"model warning {n}" for n in range(interp.WARNINGS_MAX)]
    result["warnings"] = model_warnings
    calls.clear()
    review = {"text_steer": interp.judgment.REVIEW_STEER,
              "meta_steer": 0.0, "severity": 0.0}
    with mock.patch.object(interp.judgment, "available", return_value=True), \
         mock.patch.object(interp.judgment, "screen_material", return_value=review), \
         mock.patch.object(interp, "post", side_effect=fake_post):
        findings = interp.interpret(
            {"url": "https://example.com/screened", "text": "body",
             "assets": [], "links": [], "metadata": {}}, config, [])
    assert "screening: suspicious content flagged; proceeding" in findings["warnings"]
    assert set(model_warnings).issubset(findings["warnings"])
    assert len(findings["warnings"]) <= 100


def test_screening_warning_projection():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")
    import django
    django.setup()
    from clipshelf.models import Job

    warning = "screening: suspicious content flagged; proceeding"
    job = Job(warnings=[warning, "unverified: https://example.com", warning,
                        "screening unavailable: offline"])
    assert job.screening_warnings == [
        warning, "screening unavailable: offline"]
def test_direct_media_single_fetch():
    """Direct media arrives in ONE fetch under the media cap; pages keep the
    page cap. Before the content-type cap this rejected valid direct video
    over PAGE_MAX_BYTES and downloaded small media twice."""
    tmp = tempfile.mkdtemp(prefix="cs-dir-")
    real_page_cap = acq.PAGE_MAX_BYTES
    real_validate = acq.validate_video_file
    big_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * (8192 - 12)
    small_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * (2048 - 12)
    acq.PAGE_MAX_BYTES = 4096
    acq.validate_video_file = lambda path: {"duration": 1.0}
    try:
        # direct video over the page cap but under the media cap is acquired
        fake, original = patched({"media.example": [PUBLIC]},
                                 [http(200, "video/mp4", big_mp4),
                                  http(200, "video/mp4", big_mp4)])
        try:
            src = acq.acquire("http://media.example/movie.mp4", tmp)
            assert src["acquisition"] == "complete", src["warnings"]
            assert src["metadata"]["media"] == "video"
            assert len(fake.connected) == 1, fake.connected
            with open(src["assets"][0]["path"], "rb") as fh:
                assert fh.read() == big_mp4
        finally:
            net.socket = original
        # small direct media: one GET, not a page fetch plus a media re-fetch
        fake, original = patched({"media.example": [PUBLIC]},
                                 [http(200, "video/mp4", small_mp4),
                                  http(200, "video/mp4", small_mp4)])
        try:
            src = acq.acquire("http://media.example/clip.mp4", tmp)
            assert src["acquisition"] == "complete", src["warnings"]
            assert len(fake.connected) == 1, fake.connected
        finally:
            net.socket = original
        # images likewise single-fetch
        fake, original = patched({"media.example": [PUBLIC]},
                                 [http(200, "image/png", png_bytes()),
                                  http(200, "image/png", png_bytes())])
        try:
            src = acq.acquire("http://media.example/pic.png", tmp)
            assert src["acquisition"] == "complete", src["warnings"]
            assert src["metadata"]["media"] == "image"
            assert len(fake.connected) == 1, fake.connected
        finally:
            net.socket = original
        # plain pages keep the page byte cap
        fake, original = patched({"media.example": [PUBLIC]},
                                 [http(200, "text/html", b"<html>" + b"x" * 8192)])
        try:
            src = acq.acquire("http://media.example/page.html", tmp)
            assert src["acquisition"] == "error", src["acquisition"]
        finally:
            net.socket = original
    finally:
        acq.PAGE_MAX_BYTES = real_page_cap
        acq.validate_video_file = real_validate
        shutil.rmtree(tmp, ignore_errors=True)


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
    assert report["ok"] is False and set(report) == {"ok", "message"}, report

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
        else:
            raise AssertionError("malformed response accepted after retry")
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
        assert report == {"ok": False,
                          "message": "connect failed for key *** at host"}, report
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
        assert report == {"ok": True, "message": "image+JSON ok via m (saw: red)"}, report
        assert calls[-1]["temperature"] == 0  # deterministic probe
        assert [m["role"] for m in calls[-1]["messages"]] == ["system", "user"]
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

    def check_offline():
        with mock.patch.object(net, "fetch_public",
                               side_effect=net.NetworkError("offline")):
            check()
    return unittest.FunctionTestCase(check_offline)


def test_post_transport_and_http_errors():
    """post(): one POST to <base>/chat/completions with bearer auth; honest,
    key-redacted HTTP errors; responses above the size bound are refused."""
    calls = []

    def fake_open(req, timeout=None):
        calls.append((req.full_url, req.get_method(),
                      req.get_header("Authorization")))
        return _Resp(b'{"choices": []}')

    real_open = interp.urllib.request.urlopen
    interp.urllib.request.urlopen = fake_open
    try:
        assert interp.post("http://h/v1", "sk-test", {"q": 1}, 5) == {"choices": []}
        assert calls[-1] == ("http://h/v1/chat/completions", "POST", "Bearer sk-test")
    finally:
        interp.urllib.request.urlopen = real_open

    # HTTP 401: credentials surfaced, key never leaks; body has undecodable
    # bytes so the detail decode actually runs its error handler
    def denied(req, timeout=None):
        raise interp.urllib.error.HTTPError(req.full_url, 401, "no", {},
                                            io.BytesIO(b"bad key sk-test \xff\xfe"))

    interp.urllib.request.urlopen = denied
    try:
        try:
            interp.post("http://h/v1", "sk-test", {}, 5)
            raise AssertionError("401 accepted")
        except interp.InterpretationError as exc:
            assert "credentials" in str(exc) and "sk-test" not in str(exc)
    finally:
        interp.urllib.request.urlopen = real_open

    # HTTP 500: detail is redacted and truncated at its 300-char bound
    def err500(req, timeout=None):
        body = b"X" * 10 + b" sk-test " + b"B" * 400
        raise interp.urllib.error.HTTPError(req.full_url, 500, "boom", {},
                                            io.BytesIO(body))

    interp.urllib.request.urlopen = err500
    try:
        try:
            interp.post("http://h/v1", "sk-test", {}, 5)
            raise AssertionError("500 accepted")
        except interp.InterpretationError as exc:
            msg = str(exc)
            assert "500" in msg and "sk-test" not in msg and "***" in msg, msg
            assert len(msg) <= len("endpoint rejected request (HTTP 500): ") + 300, msg
    finally:
        interp.urllib.request.urlopen = real_open

    # a response above the size bound is refused, not parsed
    def huge(req, timeout=None):
        return _Resp(b"x" * 40)

    real_max = interp.LLM_RESPONSE_MAX
    interp.LLM_RESPONSE_MAX = 16
    interp.urllib.request.urlopen = huge
    try:
        try:
            interp.post("http://h/v1", None, {}, 5)
            raise AssertionError("oversized response accepted")
        except interp.InterpretationError as exc:
            assert "size bound" in str(exc)
    finally:
        interp.LLM_RESPONSE_MAX = real_max
        interp.urllib.request.urlopen = real_open

    # list_models on an unreachable endpoint: transport reported, key redacted
    def unreachable(req, timeout=None):
        raise interp.urllib.error.URLError("refused")

    interp.urllib.request.urlopen = unreachable
    try:
        try:
            interp.list_models({"base_url": "http://h/v1", "api_key": "sk-test"})
            raise AssertionError("unreachable accepted")
        except interp.ConfigurationError as exc:
            assert "endpoint unreachable: <" in str(exc)
            assert "sk-test" not in str(exc)
    finally:
        interp.urllib.request.urlopen = real_open
    print("post transport ok")


def test_image_source_bounds():
    """Source images above the 32 MiB bound are skipped before decoding with
    an honest warning; a file exactly at the bound still decodes to one part."""
    tmp = tempfile.mkdtemp(prefix="cs-img-")
    try:
        warnings = []
        big = os.path.join(tmp, "big.png")
        with open(big, "wb") as fh:
            fh.seek(32 << 20)  # 32 MiB + 1 byte, sparse; size independent of the constant
            fh.write(b"\0")
        parts = interp._image_parts({"assets": [{"kind": "image", "path": big}]},
                                    warnings, interp.IMAGES_MAX)
        assert parts == [], parts
        assert any("size bound" in w for w in warnings), warnings

        at = os.path.join(tmp, "at.png")
        data = png_bytes()
        with open(at, "wb") as fh:
            fh.write(data + b"\0" * ((32 << 20) - len(data)))
        parts = interp._image_parts({"assets": [{"kind": "image", "path": at}]},
                                    warnings, interp.IMAGES_MAX)
        assert len(parts) == 1
        assert parts[0]["type"] == "image_url"
        assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

        # beyond the 16-image budget the rest is dropped with an honest count
        tiny = os.path.join(tmp, "tiny.png")
        with open(tiny, "wb") as fh:
            fh.write(data)
        many = [{"kind": "image", "path": tiny} for _ in range(17)]
        warnings = []
        parts = interp._image_parts({"assets": many}, warnings, interp.IMAGES_MAX)
        assert len(parts) == 16, len(parts)
        assert any("1 images omitted" in w for w in warnings), warnings

        # re-encode ladder: too big at (1536, 85), lands on the 1024 rung
        rng = random.Random(0)
        noise = Image.frombytes(
            "RGB", (2048, 2048),
            bytes(255 if rng.getrandbits(1) else 0
                  for _ in range(2048 * 2048 * 3)))
        buf = io.BytesIO()
        noise.save(buf, "PNG")
        url = interp._data_url(buf.getvalue(), warnings, "noise.png")
        assert url and url.startswith("data:image/jpeg;base64,"), warnings
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        assert 768 < max(im.size) <= 1024, im.size
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("image bounds ok")


def test_frame_extraction_contract():
    """ffmpeg frame sampling: file-only whitelist, bounded window/fps/frames,
    quality and scale pinned; budget 0 never spawns a subprocess."""
    source = {"assets": [{"kind": "video", "path": "C:/nonexistent/v.mp4"}]}
    runs = []

    def fake_run(argv, capture_output=True, timeout=None, check=False):
        runs.append((list(argv), capture_output, timeout, check))
        if "ffprobe" in argv[0]:
            out = json.dumps({"format": {"duration": "60"}}).encode()
            return interp.subprocess.CompletedProcess(argv, 1, stdout=out, stderr=b"")
        with open(argv[-1].replace("%02d", "00"), "wb") as fh:  # materialize a frame
            fh.write(jpeg_bytes())
        return interp.subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with mock.patch.object(interp.shutil, "which", lambda name: f"/fakebin/{name}"), \
         mock.patch.object(interp.subprocess, "run", fake_run):
        warnings = []
        parts = interp._frame_parts(source, warnings, interp.FRAMES_MAX)
        assert len(runs) == 2, runs
        probe_argv, probe_cap, probe_t, probe_check = runs[0]
        assert probe_argv == ["/fakebin/ffprobe", "-v", "error",
                              "-protocol_whitelist", "file", "-show_entries",
                              "format=duration", "-of", "json",
                              "C:/nonexistent/v.mp4"], probe_argv
        assert probe_cap is True and probe_t == 60 and probe_check is False
        ffmpeg_argv, ffmpeg_cap, ffmpeg_t, ffmpeg_check = runs[1]
        assert ffmpeg_argv[:-1] == ["/fakebin/ffmpeg", "-v", "error",
                                    "-protocol_whitelist", "file", "-t", "60.0",
                                    "-i", "C:/nonexistent/v.mp4", "-an", "-sn",
                                    "-dn", "-vf",
                                    "fps=8/60,scale=1280:1280:"
                                    "force_original_aspect_ratio=decrease",
                                    "-frames:v", "8", "-q:v", "5"], ffmpeg_argv
        assert ffmpeg_argv[-1].endswith("f%02d.jpg")
        assert ffmpeg_cap is True and ffmpeg_t == 180 and ffmpeg_check is False
        assert warnings == [], warnings  # 60s sits under the duration bound
        assert len(parts) == 1
        assert parts[0]["type"] == "image_url"
        assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

        # budget 0: frames skipped wholesale, no subprocess ever launched
        runs.clear()
        warnings = []
        assert interp._frame_parts(source, warnings, 0) == []
        assert runs == [], runs
        assert any("video frames omitted" in w for w in warnings), warnings
    print("frame contract ok")


def test_material_state_shape():
    """Only the current capture's bounded context is shipped to the model."""
    cats = [f"cat{i}" for i in range(101)]
    source = {"url": "https://example.com/p", "title": "tt", "desc": "d" * 4001,
              "text": "b" * 12001,
              "links": [{"url": "https://a/1", "title": "x" * 250}, "junk", 42,
                        {"no": "url"}],
              "metadata": {"media": "video", "captions": "none"}}
    state = interp._material_state(source, cats)
    assert state["url"] == "https://example.com/p"
    assert state["title"] == "tt"
    assert state["description"] == "d" * 4000
    assert state["page_text"] == "b" * 12000
    assert state["links"] == [{"url": "https://a/1", "title": "x" * 200}]
    assert "no captions" in state["captions_note"]
    assert len(state["allowed_categories"]) == 100

    # captions note only for caption-less video captures
    talking = interp._material_state(
        {**source, "metadata": {"media": "video", "captions": "en"}}, cats)
    assert talking["captions_note"] == ""
    plain = interp._material_state({"url": "https://a", "metadata": {}}, cats[:2])
    assert plain["captions_note"] == ""
    assert plain["allowed_categories"] == ["cat0", "cat1"]
    print("material state ok")


def test_parse_json_content_forms():
    """Bare, fenced, and prose-wrapped JSON objects parse; junk is rejected."""
    assert interp._parse_json_content('{"a": 1}') == {"a": 1}
    assert interp._parse_json_content('```json\n{"a": 2}\n```') == {"a": 2}
    assert interp._parse_json_content('```\n{"a": 5}\n```') == {"a": 5}
    assert interp._parse_json_content('Answer:{"a": 3}!') == {"a": 3}
    for bad in ("no object here", "}{", ""):
        try:
            interp._parse_json_content(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    print("parse forms ok")


def test_validated_findings_caps():
    """Findings lists are capped (repos/warnings/categories at 10), the prompt
    entry bound is 4000 chars, and the result shape is exact."""
    cats = [f"c{i}" for i in range(12)]
    source_url = "https://example.com/p"

    def raw(**over):
        base = {"summary": "s", "repos": [f"o/r{i}" for i in range(11)],
                "prompts": ["p" * 4000], "links": [],
                "categories": [f"c{i}" for i in range(11)], "installs": [],
                "warnings": [f"w{i}" for i in range(11)]}
        base.update(over)
        return base

    out = interp._validated(raw(), source_url, cats, [])
    assert out == {"source": source_url, "summary": "s",
                   "repos": [f"https://github.com/o/r{i}" for i in range(10)],
                   "prompts": ["p" * 4000], "links": [],
                   "categories": [f"c{i}" for i in range(10)],
                   "installs": [], "warnings": [f"w{i}" for i in range(10)]}

    # one char past the prompt bound is fatal, and names the offending list
    try:
        interp._validated(raw(prompts=["p" * 4001]), source_url, cats, [])
        raise AssertionError("over-long prompt accepted")
    except ValueError as exc:
        assert "prompts entry exceeds 4000 chars" in str(exc)

    # an off-collection category is dropped with a bounded-name warning
    warnings = []
    out = interp._validated(raw(categories=["x" * 60]), source_url, cats, warnings)
    assert out["categories"] == []
    assert any("'" + "x" * 50 + "'" in w for w in warnings), warnings
    print("findings caps ok")


def test_verify_urls_policy():
    """Emitted URLs are re-fetched once each (deduped, http(s) only); past 20
    the rest are skipped with an honest count."""
    fetched = []

    def failing_fetch(url, **k):
        fetched.append(url)
        raise net.NetworkError("down")

    real_fetch = net.fetch_public
    net.fetch_public = failing_fetch
    try:
        warnings = []
        findings = {"repos": ["https://a/1", "https://a/1"],
                    "links": ["https://b/2", "owner/name"], "warnings": []}
        interp._verify_urls(findings, warnings)
        assert fetched == ["https://a/1", "https://b/2"], fetched
        assert sorted(findings["warnings"]) == ["unverified: https://a/1",
                                                "unverified: https://b/2"]
        assert warnings == [], warnings

        fetched.clear()
        findings = {"repos": [],
                    "links": [f"https://h/{i}" for i in range(21)],
                    "warnings": []}
        warnings = []
        interp._verify_urls(findings, warnings)
        assert len(fetched) == 20, len(fetched)
        assert warnings == ["1 URLs not re-verified"], warnings

        fetched.clear()
        findings = {"repos": [], "links": ["https://c/1"], "warnings": []}
        warnings = []
        interp._verify_urls(findings, warnings)
        assert len(fetched) == 1
        assert not any("not re-verified" in w for w in warnings), warnings
    finally:
        net.fetch_public = real_fetch
    print("verify urls ok")


def test_interpret_pipeline_contract():
    """One POST with the exact chat contract; unknown reply keys surface in
    warnings; a url-less source is refused without any endpoint call."""
    categories = ["github"]
    source = {"url": "https://example.com/page", "title": "T", "desc": "D",
              "text": "body", "links": [], "assets": [], "metadata": {}}
    cfg = {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": None}
    extra_raw = ('{"summary": "s", "repos": ["owner/repo"], "prompts": [], '
                 '"links": [], "categories": ["github"], "installs": [], '
                 '"warnings": [], "bonus": 1}')
    calls = []

    def spy_post(base, key, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": extra_raw}}]}

    real_fetch = net.fetch_public
    net.fetch_public = lambda url, **k: (_ for _ in ()).throw(net.NetworkError("down"))
    real_post = interp.post
    interp.post = spy_post
    try:
        findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert any("ignored unexpected keys: ['bonus']" in w
                   for w in findings["warnings"]), findings["warnings"]
        assert len(calls) == 1
        payload = calls[0]
        assert payload["model"] == "m" and payload["temperature"] == 0.2
        assert payload["max_tokens"] == interp.OUTPUT_TOKENS_MAX
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        text_part = payload["messages"][1]["content"][0]
        assert text_part["type"] == "text"
        assert text_part["text"].startswith(
            'Capture material (untrusted JSON data):\n'
            '{"url": "https://example.com/page"'), text_part["text"][:80]
        assert text_part["text"].endswith("}")
    finally:
        interp.post = real_post
        net.fetch_public = real_fetch

    try:
        interp.interpret({"nope": 1}, cfg, categories)
        raise AssertionError("url-less source accepted")
    except interp.InterpretationError as exc:
        assert str(exc).endswith("url is required")
    print("pipeline contract ok")


def test_interpret_screening_thresholds():
    """Steer thresholds: below review passes silently, mid steer flags but
    preserves findings, zero-steer screening changes nothing."""
    categories = ["github"]
    source = {"url": "https://example.com/page", "title": "T", "desc": "D",
              "text": "body", "links": [], "assets": [], "metadata": {}}
    cfg = {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": None}
    good_raw = ('{"summary": "s", "repos": ["owner/repo"], "prompts": [], '
                '"links": [], "categories": ["github"], "installs": ["pip i x"], '
                '"warnings": []}')

    def ok_post(base, key, payload, timeout):
        return {"choices": [{"message": {"content": good_raw}}]}

    def screen(steer, sev=0.0, meta=None):
        return lambda state, api_key=None: {"text_steer": steer,
                                            "meta_steer": meta, "severity": sev}

    real_fetch = net.fetch_public
    net.fetch_public = lambda url, **k: (_ for _ in ()).throw(net.NetworkError("down"))
    try:
        # zero steer with a missing meta answer: findings pass through intact
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material",
                               screen(0.0, meta=None)), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert findings["installs"] == ["pip i x"]
        assert not any("suspicious" in w for w in findings["warnings"]), \
            findings["warnings"]

        # steer below the review threshold: screened, but no flag
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material",
                               screen(0.10)), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert not any("suspicious content flagged" in w
                       for w in findings["warnings"]), findings["warnings"]

        # mid steer (review..action): flagged, findings intact
        with mock.patch.object(interp.judgment, "available", return_value=True), \
             mock.patch.object(interp.judgment, "screen_material",
                               screen(0.50)), \
             mock.patch.object(interp, "post", ok_post):
            findings = interp.interpret(source, cfg, categories)
        assert findings["repos"] == ["https://github.com/owner/repo"]
        assert any("suspicious content flagged" in w
                   for w in findings["warnings"]), findings["warnings"]
    finally:
        net.fetch_public = real_fetch
    print("screening thresholds ok")


def test_check_connection_contract():
    """The probe ships a system contract + color question + inline PNG; the
    report shape is honest in all outcomes and never leaks the key."""
    cfg = {"base_url": "http://h/v1", "model": "m", "api_key": "sk-check"}
    real_post = interp.post

    def offcontract_post(base, key, payload, timeout):
        return {"choices": [{"message": {"content": '{"ok": false, "color": "red"}'}}]}

    interp.post = offcontract_post
    try:
        report = interp.check_connection(cfg)
        assert report["ok"] is False
        assert report["message"].startswith(
            "model replied but not with the JSON contract"), report["message"]
    finally:
        interp.post = real_post

    calls = []

    def blue_post(base, key, payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": '{"ok": true, "color": "blue"}'}}]}

    interp.post = blue_post
    try:
        report = interp.check_connection(cfg)
        assert report["ok"] is True
        assert "blue" in report["message"]
        assert ("via " + cfg["model"]) in report["message"], report["message"]
        assert report["message"].endswith(")"), report["message"]
        messages = calls[0]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"].endswith("}")
        assert '"color": "<dominant color' in messages[0]["content"]
        user = messages[1]["content"]
        assert user[0]["type"] == "text"
        assert user[0]["text"].endswith("?")
        assert user[1]["type"] == "image_url"
        assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        interp.post = real_post

    def leak_post(base, key, payload, timeout):
        raise Exception(f"connect failed for key {key}")

    interp.post = leak_post
    try:
        report = interp.check_connection(cfg)
        assert report["ok"] is False
        assert "sk-check" not in report["message"] and "***" in report["message"]
    finally:
        interp.post = real_post

    report = interp.check_connection({"model": "m"})
    assert report["ok"] is False
    assert isinstance(report["message"], str) and report["message"]
    print("check connection ok")


def test_list_models():
    """The admin picker's source of truth: what the endpoint says it has."""
    calls = []

    class Resp:
        def __init__(self, body): self.body = body
        def read(self, n): return self.body[:n]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization"), req.get_method()))
        return Resp(json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"},
                                         {"id": 7}, {"id": " "}, {}, None, "junk"]}).encode())

    real_open = interp.urllib.request.urlopen
    interp.urllib.request.urlopen = fake_open
    try:
        # sorted, deduped, non-string ids dropped; trailing slash not doubled
        assert interp.list_models({"base_url": "http://h/v1/", "api_key": "sekret"}) == ["a", "b"]
        assert calls[0] == ("http://h/v1/models", "Bearer sekret", "GET")
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

        def broken(req, timeout=None):
            raise interp.urllib.error.HTTPError(req.full_url, 500, "no", {}, io.BytesIO(b""))

        interp.urllib.request.urlopen = broken
        try:
            interp.list_models({"base_url": "http://h/v1"})
            raise AssertionError("500 accepted")
        except interp.ConfigurationError as exc:
            assert str(exc) == "endpoint cannot list models (HTTP 500)", exc

        # chat transport: both auth refusals read as credential errors
        for code in (401, 403):
            def refused(req, timeout=None, code=code):
                raise interp.urllib.error.HTTPError(
                    req.full_url, code, "no", {}, io.BytesIO(b"sekret detail"))

            interp.urllib.request.urlopen = refused
            try:
                interp.post("http://h/v1", "sekret", {}, 5)
                raise AssertionError(f"{code} accepted")
            except interp.InterpretationError as exc:
                assert str(exc) == f"endpoint rejected credentials (HTTP {code})", exc

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
    test_deadline_bounded_reads()
    print("deadline bounds ok")
    tmp = tempfile.mkdtemp(prefix="cs-src-")
    try:
        test_import_media_bounds(tmp)
        test_acquisition_regressions(tmp)
        test_interpretation_regressions(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    test_direct_media_single_fetch()
    print("direct media ok")
    test_interpretation_bounds()
    test_screening_policy().debug()
    test_screening_warning_projection()
    test_post_transport_and_http_errors()
    test_image_source_bounds()
    test_frame_extraction_contract()
    test_material_state_shape()
    test_parse_json_content_forms()
    test_validated_findings_caps()
    test_verify_urls_policy()
    test_interpret_pipeline_contract()
    test_interpret_screening_thresholds()
    test_check_connection_contract()
    test_list_models()
    print("ok")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.FunctionTestCase(test)])


if __name__ == "__main__":
    test()
