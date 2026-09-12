"""SSRF-safe public fetch and bounded egress for extractors.

fetch_public() resolves destinations itself, refuses any non-public address
(including IPv6-mapped/6to4/Teredo embeddings), pins the validated address at
connect time so DNS rebinding cannot switch it, re-validates every redirect
hop, and bounds decompression, total bytes, and wall time.

Extractor traffic (gallery-dl, yt-dlp, including curl_cffi impersonation,
which bypasses Python's resolver) is forced through extractor_egress(): a
loopback-only CONNECT proxy that applies the identical public-destination
policy per tunnel and pins the upstream IP. Extractors receive it as a fixed
--proxy argument; no ambient config, cookies, or env proxies are honored.
"""
import contextlib
import http.client
import ipaddress
import os
import select
import socket
import socketserver
import ssl
import threading
import time
import urllib.parse
import zlib

__all__ = ["NetworkError", "fetch_public", "resolve_public", "extractor_egress",
           "scrub_env"]

MAX_REDIRECTS = 10
DEFAULT_MAX_BYTES = 8 << 20      # decompressed body ceiling for fetch_public
DEFAULT_TIMEOUT = 30             # whole-fetch wall clock, seconds
HDR_CAP = 64 << 10
TUNNEL_CAP = 4 << 30             # coarse whole-tunnel byte cap (ponytail: TLS
                                 # overhead counts; tighten per-URL if needed)
TUNNEL_TIMEOUT = 1800
TUNNEL_SLOTS = 16                # concurrent extractor connections

UA_BROWSER = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
UA_FACEBOOK = "facebookexternalhit/1.1"   # shortlink resolvers per tiktok research

_TLS = {"https": ssl.create_default_context()}
_SLOTS = threading.BoundedSemaphore(TUNNEL_SLOTS)


class NetworkError(Exception):
    """Refused or failed public fetch; message is safe to surface."""


# ---------------------------------------------------------------- validation

def _assert_public_ip(text):
    """Refuse loopback/private/link-local/reserved/multicast/embedded addresses."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise NetworkError(f"malformed address refused: {text[:64]!r}")
    check = ip
    if isinstance(ip, ipaddress.IPv6Address):
        # belt over 3.13's patched is_global: unwrap embedded IPv4 explicitly
        emb = ip.ipv4_mapped or ip.sixtofour
        if emb is not None:
            check = emb
    if not ip.is_global or not check.is_global:
        raise NetworkError(f"non-public destination refused: {text[:64]}")


def resolve_public(host, port):
    """All SOCK_STREAM addresses for host; every one must be globally routable.

    Mixed public+private records fail closed, so a rebinding resolver can never
    sneak a private address past the pin.
    """
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkError(f"cannot resolve {host[:255]!r}: {exc}")
    out = []
    for family, _type, _proto, _canon, sockaddr in infos:
        _assert_public_ip(sockaddr[0])
        if (family, sockaddr) not in out:
            out.append((family, sockaddr))
    if not out:
        raise NetworkError(f"no usable addresses for {host[:255]!r}")
    return out


def validate_url(url):
    """Scheme/host/port/credential checks + validated pinned addresses.

    Returns (scheme, host, port, request_path, [(family, sockaddr), ...]).
    Raises NetworkError on any refusal.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise NetworkError(f"malformed URL: {exc}")
    if parts.scheme not in ("http", "https"):
        raise NetworkError(f"unsupported scheme {parts.scheme!r}")
    if parts.username or parts.password:
        raise NetworkError("URL credentials refused")
    host = parts.hostname
    if not host:
        raise NetworkError("URL has no host")
    default = 443 if parts.scheme == "https" else 80
    if port is None:
        port = default
    elif port != default:
        raise NetworkError(f"non-standard port {port} refused")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.scheme, host, port, path, resolve_public(host, port)


# ------------------------------------------------------------- pinned fetch

class _PinnedHTTP(http.client.HTTPConnection):
    """HTTPConnection that connects to one pre-validated sockaddr, never DNS."""

    def __init__(self, host, port, family, sockaddr, timeout, tls_ctx):
        super().__init__(host, port, timeout=timeout)
        self._family, self._sockaddr, self._tls_ctx = family, sockaddr, tls_ctx

    def connect(self):
        self.sock = socket.socket(self._family, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._sockaddr)
        if self._tls_ctx is not None:
            self.sock = self._tls_ctx.wrap_socket(self.sock, server_hostname=self.host)
            self.sock.settimeout(self.timeout)


def _open_pinned(scheme, host, port, path, infos, user_agent, timeout):
    headers = {"Host": host if port in (80, 443) else f"{host}:{port}",
               "User-Agent": user_agent, "Accept": "*/*",
               "Accept-Encoding": "gzip, deflate", "Connection": "close"}
    last = None
    for family, sockaddr in infos:
        conn = _PinnedHTTP(host, port, family, sockaddr, timeout, _TLS.get(scheme))
        try:
            conn.request("GET", path, headers=headers)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            conn.close()
    raise NetworkError(f"connect to {host[:255]} failed: {last}")


def _bounded_read(resp, encoding, max_bytes, deadline):
    """Decompression-bounded read; gzip/deflate bombs die at max_bytes."""
    enc = (encoding or "").strip().lower()
    dec = None
    if enc in ("gzip", "x-gzip"):
        dec = zlib.decompressobj(47)          # auto zlib/gzip header
    elif enc in ("deflate", "zlib"):
        dec = zlib.decompressobj(47)
    out = bytearray()

    def room():
        return max_bytes - len(out)

    def absorb(piece):
        out.extend(piece)
        if len(out) > max_bytes:
            raise NetworkError(f"response exceeds {max_bytes} byte bound")

    try:
        while True:
            if dec is not None and dec.unconsumed_tail:
                absorb(dec.decompress(dec.unconsumed_tail, 65536))
                continue
            chunk = resp.read(65536)
            if not chunk:
                break
            if time.monotonic() > deadline:
                raise NetworkError("fetch exceeded time bound")
            if dec is None:
                absorb(chunk)
            else:
                absorb(dec.decompress(chunk, 65536))
        if dec is not None:
            tail = dec.flush(max(room(), 0) + 1) if room() >= 0 else b""
            if tail:
                out.extend(tail)
                if len(out) > max_bytes:
                    raise NetworkError(f"response exceeds {max_bytes} byte bound")
            if not dec.eof:
                raise NetworkError("truncated compressed response")
    except zlib.error as exc:
        raise NetworkError(f"malformed compressed response: {exc}")
    return bytes(out)


def _media_type(content_type):
    return (content_type or "").split(";")[0].strip().lower()


def fetch_public(url, max_bytes=DEFAULT_MAX_BYTES, timeout=DEFAULT_TIMEOUT,
                 user_agent=UA_BROWSER):
    """Validated, IP-pinned public GET.

    Returns {"url": final URL, "body": bytes (decompressed), "content_type":
    lowercased media type}. Every redirect hop is re-validated; decompression,
    total bytes, and wall time are bounded. Raises NetworkError.
    """
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    deadline = time.monotonic() + timeout
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, path, infos = validate_url(current)
        remaining = max(1.0, deadline - time.monotonic())
        conn, resp = _open_pinned(scheme, host, port, path, infos, user_agent, remaining)
        try:
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                _bounded_read(resp, None, min(1 << 20, max_bytes), deadline)
                if not location:
                    raise NetworkError(f"redirect from {host[:255]} without location")
                current = urllib.parse.urljoin(current, location)
                continue
            if not 200 <= status < 300:
                _bounded_read(resp, None, 1 << 20, deadline)
                raise NetworkError(f"HTTP {status} from {host[:255]}")
            body = _bounded_read(resp, resp.headers.get("Content-Encoding"),
                                 max_bytes, deadline)
            return {"url": current, "body": body,
                    "content_type": _media_type(resp.headers.get("Content-Type"))}
        finally:
            conn.close()
    raise NetworkError(f"more than {MAX_REDIRECTS} redirects")


# ------------------------------------------------- bounded extractor proxy

def _reply(sock, status, reason):
    with contextlib.suppress(OSError):
        sock.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n"
                     f"Connection: close\r\n\r\n".encode("ascii"))


def _connect_pinned(infos, timeout):
    last = None
    for family, sockaddr in infos:
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise NetworkError(f"upstream connect failed: {last}")


def _relay(client, upstream, deadline):
    """Bidirectional tunnel copy with wall-time and byte bounds."""
    seen, socks = 0, [client, upstream]
    while socks:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        ready, _, _ = select.select(socks, [], [], min(left, 30))
        for sock in ready:
            try:
                data = sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            seen += len(data)
            if seen > TUNNEL_CAP:
                return
            try:
                (upstream if sock is client else client).sendall(data)
            except OSError:
                return


_DROP_HEADERS = ("connection", "proxy-connection", "proxy-authorization",
                 "keep-alive", "te", "upgrade")


class _ProxyHandler(socketserver.BaseRequestHandler):
    """One client connection: CONNECT tunnel or absolute-form http forward."""

    def handle(self):
        if not _SLOTS.acquire(timeout=5):
            _reply(self.request, 503, "busy")
            return
        try:
            self.request.settimeout(TUNNEL_TIMEOUT)
            first, rest = self._read_head()
            self._dispatch(first, rest)
        except (OSError, ValueError, NetworkError) as exc:
            _reply(self.request, 502, "egress refused")
            with contextlib.suppress(Exception):
                self.request.sendall(f"clipshelf-proxy: {exc}\n".encode()[:512])
        finally:
            _SLOTS.release()
            with contextlib.suppress(OSError):
                self.request.close()

    def _read_head(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > HDR_CAP:
                raise ValueError("request headers exceed bound")
            chunk = self.request.recv(8192)
            if not chunk:
                raise ValueError("client closed before request")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _dispatch(self, head, rest):
        try:
            method, target, _ver = head.decode("latin1").split(" ", 2)
        except ValueError:
            _reply(self.request, 400, "bad request")
            return
        deadline = time.monotonic() + TUNNEL_TIMEOUT
        if method == "CONNECT":
            host, _, sport = target.rpartition(":")
            port = int(sport) if sport.isdigit() else -1
            if port not in (80, 443):
                _reply(self.request, 403, "port refused")
                return
            infos = resolve_public(host, port)   # raises NetworkError -> 502
            _reply(self.request, 200, "Connection established")
            try:
                upstream = _connect_pinned(infos, 30)
            except NetworkError:
                return
            try:
                _relay(self.request, upstream, deadline)
            finally:
                upstream.close()
            return
        if method in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH") \
                and target.startswith("http://"):
            self._forward_http(method, target, head, rest, deadline)
            return
        _reply(self.request, 405, "only CONNECT or absolute-form http")

    def _forward_http(self, method, target, head, rest, deadline):
        _scheme, host, port, path, infos = validate_url(target)
        lines = head.decode("latin1").split("\r\n")
        out = [f"{method} {path} HTTP/1.1"]
        clen = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            key = name.strip().lower()
            if key in _DROP_HEADERS or key == "host":
                continue
            if key == "content-length":
                clen = int(value.strip())
                if clen < 0 or clen > TUNNEL_CAP:
                    raise ValueError("request body exceeds bound")
            if key == "transfer-encoding":
                raise ValueError("chunked request bodies unsupported")
            out.append(line)
        out += [f"Host: {host}", "Connection: close", ""]
        payload = "\r\n".join(out).encode("latin1") + b"\r\n" + rest[:clen]
        # drain any body bytes read past the header block
        while len(rest) < clen:
            chunk = self.request.recv(min(65536, clen - len(rest)))
            if not chunk:
                break
            rest += chunk
            payload += chunk
        upstream = _connect_pinned(infos, 30)
        try:
            upstream.sendall(payload)
            while len(rest) < clen:      # keep draining if body was long
                chunk = self.request.recv(65536)
                if not chunk:
                    break
                rest += chunk
                upstream.sendall(chunk)
            _relay(self.request, upstream, deadline)   # response back, to EOF
        finally:
            upstream.close()


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


@contextlib.contextmanager
def extractor_egress():
    """Yield a loopback proxy URL that enforces the public-fetch policy on all
    extractor traffic. Bind with fixed --proxy args; never from ambient env."""
    server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def scrub_env(base=None):
    """Environment copy without ambient proxy variables (explicit args only)."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.lower().endswith("_proxy")]:
        env.pop(key)
    return env


