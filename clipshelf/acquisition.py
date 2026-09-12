"""DB-free acquisition: public pages, TikTok, and browser-export imports.

acquire(url, directory) returns the contract source dict; import_export(items,
directory) normalizes tiktok.json / staged-media items into the same shape.
All network traffic goes through clipshelf.network (pinned, bounded). Extractors
run with fixed argument arrays through the bounded loopback proxy; no user
cookies, no ambient extractor configs, no arbitrary tool options, and ffmpeg/
ffprobe only ever touch local published files.
"""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from PIL import Image

from clipshelf import network
from clipshelf.lib import TEXT_LIMIT, PageParser, is_short, norm

__all__ = ["AcquisitionError", "acquire", "import_export", "validate_image",
           "validate_video_file"]

PAGE_MAX_BYTES = 8 << 20          # HTML/JSON page fetch ceiling
MEDIA_MAX_BYTES = 256 << 20       # direct image/video/audio link ceiling
IMPORT_MEDIA_MAX_BYTES = 96 << 20 # per embedded/imported media file ceiling
IMPORT_MAX_ITEMS = 500
EXTRACTOR_TIMEOUT = 900           # per extractor subprocess run, seconds
VIDEO_MAX_DURATION = 900          # seconds of video we are willing to keep
IMAGE_MAX_PIXELS = 16384          # per-axis decoded image bound

IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif", "avif": "image/avif"}
VIDEO_TYPES = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
               "m4v": "video/x-m4v", "mkv": "video/x-matroska"}
AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
               "ogg": "audio/ogg", "opus": "audio/opus", "wav": "audio/wav"}
CAPTION_EXTS = {"srt", "vtt"}
_IMAGE_FORMAT_TYPE = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
                      "GIF": "image/gif", "AVIF": "image/avif"}


class AcquisitionError(Exception):
    """Invalid or oversized input; per-URL failures become source status."""


def new_source(original_url):
    return {"url": norm(original_url) or original_url, "original_url": original_url,
            "title": "", "desc": "", "text": "", "links": [], "assets": [],
            "acquisition": "error", "warnings": [], "metadata": {}}


def _warn(source, message):
    message = str(message)[:500]
    if message not in source["warnings"]:
        source["warnings"].append(message)


# ---------------------------------------------------------------- assets

def _safe_name(name, fallback="file"):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(str(name or ""))).strip("._")
    return (name or fallback)[:80]


def _target_path(directory, filename, source):
    target = os.path.join(directory, f"{len(source['assets']):02d}_{_safe_name(filename)}")
    if os.path.exists(target):  # per-job dirs are fresh; this is belt for reuse
        stem, ext = os.path.splitext(target)
        target = f"{stem}_{abs(hash(source['original_url'])) % 99999:05d}{ext}"
    if os.path.realpath(directory) != os.path.commonpath(
            [os.path.realpath(directory), os.path.realpath(target)]):
        raise AcquisitionError("asset path escaped directory")
    return target


def store_bytes(source, directory, filename, data, kind, content_type):
    target = _target_path(directory, filename, source)
    with open(target, "wb") as fh:
        fh.write(data)
    source["assets"].append({"path": target, "kind": kind,
                             "content_type": content_type, "position": len(source["assets"])})
    return target


def adopt_file(source, src_path, directory, kind, content_type):
    target = _target_path(directory, os.path.basename(src_path), source)
    shutil.move(src_path, target)
    source["assets"].append({"path": target, "kind": kind,
                             "content_type": content_type, "position": len(source["assets"])})
    return target

# -------------------------------------------------------------- validators

def validate_image(data):
    """Full decode bounds check; returns content type or raises ValueError."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if max(im.size) > IMAGE_MAX_PIXELS:
            raise ValueError(f"image dimensions {im.size} exceed bound")
        return _IMAGE_FORMAT_TYPE.get(im.format, f"image/{(im.format or 'unknown').lower()}")


def _which(basename):
    return shutil.which(basename)


def validate_video_file(path):
    """ffprobe the local published file; raises ValueError when unplayable.

    Returns metadata dict {duration,...}; protocol use is file-only because the
    path is a local file and ffprobe is invoked with a fixed argument array.
    """
    probe = _which("ffprobe")
    if not probe:
        raise OSError("ffprobe not installed")
    cmd = [probe, "-v", "error", "-protocol_whitelist", "file",
           "-show_entries", "format=duration:stream=codec_type", "-of", "json", path]
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise ValueError(f"ffprobe rejected file: {proc.stderr.decode(errors='replace')[:300]}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("no video stream")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if duration > VIDEO_MAX_DURATION:
        raise ValueError(f"duration {duration:.0f}s exceeds {VIDEO_MAX_DURATION}s bound")
    return {"duration": duration}


# --------------------------------------------------------------- pages

def _network_status(source, exc):
    """Honest status for a refused/failed public fetch."""
    text = str(exc)
    _warn(source, text)
    source["acquisition"] = "blocked" if "refused" in text or "non-public" in text else "error"


def _acquire_page(url, directory, source):
    resp = network.fetch_public(url, max_bytes=PAGE_MAX_BYTES)
    ctype = resp["content_type"]
    if ctype.startswith("image/"):
        data = _fetch_media(resp["url"], source, MEDIA_MAX_BYTES)
        store_bytes(source, directory, _filename_from_url(resp["url"]), data,
                    "image", ctype)
        source["metadata"].update({"media": "image", "metadata": "none",
                                   "captions": "none", "tier": "server", "failure": "none"})
        source["acquisition"] = "complete"
        return
    if ctype.startswith("video/"):
        data = _fetch_media(resp["url"], source, MEDIA_MAX_BYTES)
        path = store_bytes(source, directory, _filename_from_url(resp["url"]), data,
                           "video", ctype)
        source["metadata"].update({"media": "video", "metadata": "none",
                                   "captions": "none", "tier": "server", "failure": "none"})
        try:
            validate_video_file(path)
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            _warn(source, f"video failed validation: {exc}")
            source["acquisition"] = "partial"
            return
        source["acquisition"] = "complete"
        return
    # default: HTML/document page, parsed for title/desc/readable text/links
    parser = PageParser()
    try:
        parser.feed(resp["body"].decode("utf-8", errors="replace"))
        parser.close()
    except Exception as exc:  # malformed markup must not lose the page bytes
        _warn(source, f"page parse failed: {exc}")
    store_bytes(source, directory, _filename_from_url(resp["url"], "page.html"),
                resp["body"], "page", ctype or "application/octet-stream")
    source["title"] = parser.title.strip()[:500]
    source["desc"] = (parser.desc or "").strip()[:4000]
    source["text"] = parser.text
    seen, links = set(), []
    for href, anchor in parser.links:
        if len(links) >= 50:
            _warn(source, "page links truncated at 50")
            break
        absolute = norm(urllib.parse.urljoin(resp["url"], href))
        if absolute and absolute not in seen:
            seen.add(absolute)
            links.append({"url": absolute, "title": anchor[:300]})
    source["links"] = links
    source["metadata"].update({"media": "page", "metadata": "og" if parser.desc else "none",
                               "captions": "none", "tier": "server", "failure": "none"})
    source["acquisition"] = "complete"
    if not source["title"]:
        _warn(source, "no title found in page")


def _filename_from_url(url, fallback="file"):
    name = os.path.basename(urllib.parse.urlsplit(url).path)
    return _safe_name(name, fallback)


def _fetch_media(url, source, max_bytes):
    try:
        return network.fetch_public(url, max_bytes=max_bytes,
                                    timeout=max(network.DEFAULT_TIMEOUT, 120))["body"]
    except network.NetworkError as exc:
        raise AcquisitionError(f"media fetch failed: {exc}")


# --------------------------------------------------------------- tiktok

def _tiktok_failure(stderr_text):
    """Best-effort honest mapping of extractor errors (research §6/§7)."""
    text = (stderr_text or "").lower()
    if "10216" in text or "10222" in text or ("login" in text and "requir" in text):
        return "private"
    if "10204" in text:
        return "unavailable"
    if "10231" in text or "region" in text:
        return "blocked"
    if "challenge" in text or "please wait" in text or "captcha" in text:
        return "challenge"
    return "blocked"


def _resolve_shortlink(url, source):
    if not is_short(url) or "tiktok" not in (urllib.parse.urlsplit(url).hostname or ""):
        return url
    try:
        resp = network.fetch_public(url, max_bytes=64 << 10,
                                    user_agent=network.UA_FACEBOOK)
        return resp["url"]
    except network.NetworkError as exc:
        _warn(source, f"shortlink resolve failed: {exc}")
        return url


def _oembed(canonical, source):
    """Metadata floor so an entry is never contentless (tolerates 400)."""
    try:
        resp = network.fetch_public(
            "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(canonical, safe=""),
            max_bytes=1 << 20)
        data = json.loads(resp["body"].decode("utf-8", errors="replace"))
        source["metadata"]["metadata"] = "oembed"
        return {"title": str(data.get("title") or "")[:500],
                "author": str(data.get("author_name") or "")[:200],
                "thumbnail_url": data.get("thumbnail_url") or ""}
    except Exception:
        return {"title": "", "author": "", "thumbnail_url": ""}


def _extractor_argv(tool, proxy, outdir, url):
    """Fixed argument arrays; --ignore-config/--no-cookies kill ambient state."""
    ffmpeg = _which("ffmpeg")
    if tool == "ytdlp":
        argv = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cookies",
                "--no-cookies-from-browser", "--proxy", proxy, "--socket-timeout", "30",
                "--retries", "3", "--no-playlist", "--quiet", "--no-progress",
                "--write-subs", "--write-auto-subs", "--sub-langs", ".*",
                "--sub-format", "srt/vtt/best", "--write-info-json",
                "--max-filesize", str(IMPORT_MEDIA_MAX_BYTES),
                "--paths", outdir, "-o", "%(id)s.%(ext)s"]
        if ffmpeg:
            argv += ["--ffmpeg-location", ffmpeg]
        return argv + [url]
    return [sys.executable, "-m", "gallery_dl", "--ignore-config", "--proxy", proxy,
            "-d", outdir, "--write-metadata", "-o", "retries=2",
            "-o", "filesize-max=200M", url]


def _run_extractor(argv):
    """Bounded subprocess, scrubbed env, honest (code, tail-of-stderr) result."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=EXTRACTOR_TIMEOUT,
                              env=network.scrub_env(), check=False, shell=False)
        return proc.returncode, proc.stderr.decode(errors="replace")[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"extractor exceeded {EXTRACTOR_TIMEOUT}s time bound"


def _collect_files(outdir, source, directory):
    """Classify extractor output into contract assets + captions text/info."""
    captions_text, info = "", {}
    for root, _dirs, files in os.walk(outdir):
        for name in sorted(files):
            src = os.path.join(root, name)
            ext = name.rsplit(".", 1)[-1].lower()
            if name.endswith(".json"):
                try:
                    with open(src, encoding="utf-8", errors="replace") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        info.update(data)
                except Exception as exc:
                    _warn(source, f"metadata file unreadable: {exc}")
                continue
            if ext in CAPTION_EXTS:
                with open(src, "rb") as fh:
                    captions_text = _caption_text(fh.read())
                adopt_file(source, src, directory, "captions", "application/x-subrip")
            elif ext in IMAGE_TYPES:
                with open(src, "rb") as fh:
                    data = fh.read()
                try:
                    ctype = validate_image(data)
                except Exception as exc:
                    _warn(source, f"image {name} failed validation: {exc}")
                    continue
                adopt_file(source, src, directory, "image", ctype)
            elif ext in VIDEO_TYPES:
                ctype = VIDEO_TYPES[ext]
                with open(src, "rb") as fh:
                    header = fh.read(16)
                if len(header) < 12:
                    _warn(source, f"video {name} truncated")
                    continue
                adopt_file(source, src, directory, "video", ctype)
                try:
                    validate_video_file(source["assets"][-1]["path"])
                except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                    _warn(source, f"video failed validation: {exc}")
                    source["acquisition"] = "partial"
            elif ext in AUDIO_TYPES:
                adopt_file(source, src, directory, "audio", AUDIO_TYPES[ext])
    return captions_text, info


def _caption_text(raw):
    """srt/vtt -> bounded plain text; timestamps/cue junk stripped."""
    lines = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or re.match(
                r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}", line) or line.startswith(("NOTE", "STYLE")):
            continue
        lines.append(line)
    text = " ".join(" ".join(lines).split())[:20000]
    return text


def _media_summary(source):
    kinds = [a["kind"] for a in source["assets"]]
    if "video" in kinds:
        media = "video"
    elif kinds.count("image") > 1:
        media = f"slides({kinds.count('image')})"
    elif "image" in kinds:
        media = "image"
    elif "audio" in kinds:
        media = "audio"
    else:
        media = "none"
    return media


def _acquire_tiktok(url, directory, source):
    canonical = _resolve_shortlink(url, source)
    source["url"] = norm(canonical) or canonical
    oem = _oembed(canonical, source)
    photo = "/photo/" in urllib.parse.urlsplit(canonical).path
    order = (("gallery_dl", "ytdlp") if photo else ("ytdlp", "gallery_dl"))
    workdir = tempfile.mkdtemp(dir=directory, prefix=".dl-")
    stderr_log = ""
    try:
        with network.extractor_egress() as proxy:
            for tool in order:
                outdir = tempfile.mkdtemp(dir=workdir, prefix=f"{tool}-")
                code, err = _run_extractor(_extractor_argv(tool, proxy, outdir, canonical))
                stderr_log += "\n" + err
                captions, info = _collect_files(outdir, source, directory)
                if info:
                    source["title"] = source["title"] or str(info.get("title") or "")[:500]
                    source["desc"] = (source["desc"]
                                      or str(info.get("description") or info.get("desc") or ""))[:4000]
                if captions and not source["text"]:
                    source["text"] = captions
                    source["metadata"]["captions"] = "asr"
                if code == 0 and source["assets"]:
                    break
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    media = _media_summary(source)
    source["metadata"].update({"media": media, "metadata": source["metadata"].get("metadata", "none"),
                               "tier": "server"})
    source["title"] = source["title"] or oem["title"] or (
        f"@{oem['author']}" if oem["author"] else "")
    if media == "none" and oem["thumbnail_url"]:
        try:  # cache the signed thumbnail bytes; never store the expiring URL
            thumb = network.fetch_public(oem["thumbnail_url"], max_bytes=16 << 20)
            store_bytes(source, directory, "cover.img", thumb["body"], "image",
                        thumb["content_type"] or "image/jpeg")
        except network.NetworkError as exc:
            _warn(source, f"thumbnail fetch failed: {exc}")
    media = _media_summary(source)
    source["metadata"]["media"] = media
    if media == "audio":
        # yt-dlp on a photo post yields audio only; slides were NOT acquired
        source["acquisition"] = "partial"
        _warn(source, "audio only; primary media (slides/video) missing — "
                      "retry or browser export")
    elif media != "none":
        source["acquisition"] = ("partial" if "video failed validation"
                                 in source["warnings"] else "complete")
        if media == "video" and source["metadata"].get("captions") == "none":
            _warn(source, "no captions available; spoken content was not acquired")
    else:
        failure = _tiktok_failure(stderr_log)
        source["metadata"]["failure"] = failure
        if source["metadata"].get("metadata") == "oembed" or source["assets"]:
            source["acquisition"] = "partial"
            _warn(source, f"metadata only; media acquisition failed ({failure})")
        else:
            source["acquisition"] = "blocked" if failure != "error" else "error"
            _warn(source, f"no metadata or media acquired ({failure}); "
                          "retry or browser import")


# --------------------------------------------------------------- entry

def acquire(url, directory):
    """Public acquisition of one URL into directory; returns the source dict."""
    if not isinstance(url, str) or not norm(url):
        raise AcquisitionError(f"invalid URL: {str(url)[:200]!r}")
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    source = new_source(url)
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
        if "tiktok.com" in host:
            _acquire_tiktok(url, directory, source)
        else:
            _acquire_page(url, directory, source)
    except AcquisitionError as exc:
        _warn(source, str(exc))
        source["acquisition"] = "error"
    except network.NetworkError as exc:
        _network_status(source, exc)
    except Exception as exc:  # honest failure, never a fake success
        _warn(source, f"acquisition failed: {exc}")
        source["acquisition"] = "error"
    return source


# -------------------------------------------------------- import/export

def _decode_b64(value, cap):
    if not isinstance(value, str) or not value:
        raise AcquisitionError("empty base64 media")
    if value.startswith("data:"):  # tolerate data URLs defensively
        _head, _, value = value.partition(",")
    if len(value) > (cap * 4 // 3) + 8:
        raise AcquisitionError(f"embedded media exceeds {cap} byte bound")
    try:
        return base64.b64decode(value)
    except Exception as exc:
        raise AcquisitionError(f"malformed base64 media: {exc}")


def _media_bytes(value, cap):
    """Media bytes from {b64}|{url}|{path}|plain-URL string, bounded; None if
    the source cannot be fetched (honest per-item warning), raise on invalid."""
    if isinstance(value, str):
        value = {"url": value}
    if not isinstance(value, dict) or not value:
        raise AcquisitionError("malformed media entry")
    if "b64" in value:
        return _decode_b64(value["b64"], cap)
    if "path" in value:
        path = value["path"]
        if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isfile(path):
            raise AcquisitionError(f"staged media file missing: {str(path)[:200]!r}")
        if os.path.getsize(path) > cap:
            raise AcquisitionError(f"staged media exceeds {cap} byte bound")
        with open(path, "rb") as fh:
            return fh.read()
    url = value.get("url")
    if isinstance(url, str) and norm(url):
        try:
            return network.fetch_public(url, max_bytes=cap, timeout=120)["body"]
        except network.NetworkError:
            return None
    raise AcquisitionError("media entry has no b64/url/path")


def _import_images(item, directory, source):
    images = item.get("images") or []
    if len(images) > 100:
        raise AcquisitionError("too many images in item")
    for index, entry in enumerate(images):
        data = _media_bytes(entry, IMPORT_MEDIA_MAX_BYTES)
        if data is None:
            _warn(source, f"slide {index} could not be fetched")
            continue
        try:
            ctype = validate_image(data)
        except Exception as exc:
            _warn(source, f"slide {index} failed validation: {exc}")
            continue
        store_bytes(source, directory, f"slide{index:02d}.{ctype.split('/')[1]}",
                    data, "image", ctype)


def _import_video(item, directory, source):
    """b64/path/url/mirror candidates, container-checked before publishing."""
    entry = item.get("video")
    if not entry:
        return
    candidates = []
    if isinstance(entry, dict):
        for key in ("b64", "path", "url"):
            if entry.get(key):
                candidates.append({key: entry[key]})
                break
        candidates += [{"url": u} for u in (entry.get("urls") or [])[:5]
                       if isinstance(u, str)]
    elif isinstance(entry, str) and norm(entry):
        candidates.append({"url": entry})
    data = None
    for cand in candidates[:6]:
        try:
            data = _media_bytes(cand, IMPORT_MEDIA_MAX_BYTES)
        except AcquisitionError:
            data = None
        if data and (data[4:8] == b"ftyp" or data[:4] == b"\x1aE\xdf\xa3"):
            break
        data = None
    if not data:
        _warn(source, "video bytes unavailable from export or mirrors "
                      "(or unrecognized container)")
        return
    path = store_bytes(source, directory, "video.mp4", data, "video", "video/mp4")
    try:
        validate_video_file(path)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        os.remove(path)
        source["assets"].pop()
        _warn(source, f"video failed validation: {exc}")


def _import_item(item, directory):
    source = new_source(item.get("resolvedUrl") or item.get("url"))
    source["title"] = str(item.get("title") or "")[:500]
    if not source["title"] and item.get("author"):
        source["title"] = f"@{item['author']}"[:200]
    source["desc"] = str(item.get("desc") or "")[:4000]
    _import_images(item, directory, source)
    _import_video(item, directory, source)
    media = _media_summary(source)
    source["metadata"].update({"media": media, "metadata": "none" if media == "none" else "export",
                               "captions": "none", "tier": "browser-export",
                               "failure": "none" if media != "none" else "unavailable"})
    source["acquisition"] = "complete" if media != "none" else (
        "partial" if (source["title"] or source["desc"]) else "blocked")
    if media == "none":
        _warn(source, "browser export contained no usable media; "
                      "URL/metadata retained for retry or re-export")
    return source


def import_export(items, directory):
    """Normalize tiktok.json / staged-media items; input order preserved 1:1."""
    if not isinstance(items, list):
        raise AcquisitionError("import items must be a list")
    if len(items) > IMPORT_MAX_ITEMS:
        raise AcquisitionError(f"import exceeds {IMPORT_MAX_ITEMS} item bound")
    directory = os.path.abspath(directory)
    os.makedirs(directory, exist_ok=True)
    out = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"]:
            raise AcquisitionError("every import item needs a url")
        server_ingest = (item.get("passthrough")
                         or ("images" not in item and "video" not in item
                             and "error" not in item))
        try:
            if server_ingest:
                source = acquire(item["url"], directory)  # server tier ingests directly
                source["title"] = source["title"] or str(item.get("title") or "")[:500]
                source["desc"] = source["desc"] or str(item.get("desc") or "")[:4000]
            elif item.get("error"):
                source = new_source(item["url"])
                source["acquisition"] = "blocked"
                source["metadata"].update({"media": "none", "captions": "none",
                                           "tier": "browser-export", "failure": "unavailable"})
                _warn(source, f"browser export error: {str(item['error'])[:300]}")
            else:
                source = _import_item(item, directory)
        except AcquisitionError as exc:  # one bad item fails honestly, not the batch
            source = new_source(item["url"])
            source["acquisition"] = "error"
            _warn(source, f"import item rejected: {exc}")
        out.append(source)
    return out
