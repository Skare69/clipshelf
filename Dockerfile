# syntax=docker/dockerfile:1
# Clipshelf server image: one image, two roles (web serve / background worker).
# Runs unprivileged; the operator chooses the actual runtime UID/GID in compose.yaml.
#
# Multistage: the first stage compiles the pinned SQLite with the WAL-reset fix
# (>= 3.51.3, per https://www.sqlite.org/wal.html) and downloads the pinned
# Whisper model; its compilers and headers are discarded and only the shared
# library and the model file ship.
# Both stages pin python:3.15-slim by manifest-list digest (linux/amd64 + arm64);
# re-pin deliberately after review: docker buildx imagetools inspect python:3.15-slim
ARG SQLITE_VERSION=3.53.1

# ---- SQLite build stage: gcc/make/libc6-dev live here and are thrown away ----
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS sqlite-build
ARG SQLITE_TARBALL=sqlite-autoconf-3530100.tar.gz
ARG SQLITE_URL=https://sqlite.org/2026/sqlite-autoconf-3530100.tar.gz
ARG SQLITE_SHA256=83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f

# ca-certificates is for the HTTPS downloads below; the rest is build-only tooling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*

# Download, checksum-verify, build, and install the pinned SQLite into /usr/local.
# Refuses to proceed on any hash mismatch. The distro build inside a python image
# tag is NOT assumed fixed, so we build a known-good SQLite from the pinned
# release tarball. Verify/re-pin when bumping:
#   https://www.sqlite.org/download.html (sha256 quoted there and re-checked in build)
RUN python - <<'PY'
import hashlib, os, sys, tarfile, urllib.request

url = os.environ["SQLITE_URL"]
want = os.environ["SQLITE_SHA256"]
tarball = os.environ["SQLITE_TARBALL"]
print(f"downloading {url}", flush=True)
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha256(data).hexdigest()
if got != want:
    sys.exit(f"FATAL: {tarball} sha256 {got} != pinned {want} — do not build unverified SQLite")
print(f"verified {tarball}: {got}")
with tarfile.open(fileobj=__import__("io").BytesIO(data), mode="r:gz") as tf:
    tf.extractall("/tmp/src", filter="data")
print("extracted to /tmp/src")
PY
RUN set -eux; \
    cd "/tmp/src/${SQLITE_TARBALL%.tar.gz}"; \
    ./configure --disable-static; \
    make -j"$(nproc)"; \
    make install
# No ldconfig here: the builder runs nothing; the runtime stage keeps its own
# loader path below.

# Download and checksum-verify the pinned Whisper "small" ggml model (CLIP-171:
# local speech-to-text for videos without TikTok captions). Baked in at build:
# the runtime never downloads a model. Streams to disk while hashing (~488 MB),
# refuses any hash mismatch. Verify/re-pin when bumping:
#   https://huggingface.co/ggerganov/whisper.cpp (commit revision + file sha256)
ARG WHISPER_MODEL_FILE=ggml-small.bin
ARG WHISPER_MODEL_URL=https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.bin
ARG WHISPER_MODEL_SHA256=1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b
RUN python - <<'PY'
import hashlib, os, shutil, sys, urllib.request

url = os.environ["WHISPER_MODEL_URL"]
want = os.environ["WHISPER_MODEL_SHA256"]
name = os.environ["WHISPER_MODEL_FILE"]
dest = os.path.join("/opt/whisper", name)
part = dest + ".part"
os.makedirs("/opt/whisper", exist_ok=True)
print(f"downloading {url}", flush=True)
h = hashlib.sha256()
with urllib.request.urlopen(url, timeout=120) as resp, open(part, "wb") as out:
    while chunk := resp.read(1 << 20):
        h.update(chunk)
        out.write(chunk)
got = h.hexdigest()
if got != want:
    os.remove(part)
    sys.exit(f"FATAL: {name} sha256 {got} != pinned {want} — do not ship an unverified model")
os.replace(part, dest)
print(f"verified {name}: {got}")
PY

# ---- Runtime stage: same pinned base, no compilers, only the built shared library and the model ---
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ARG SQLITE_VERSION

# ffmpeg is the only OS package the worker needs (fixed version within the Debian
# release; invocations stay fixed-arg in clipshelf.sources).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Only the shared library ships: Python's _sqlite3 extension links soname
# libsqlite3.so.0, satisfied by the copied library and its relative soname
# symlinks at import time. Headers, the static archive, and the sqlite3 CLI
# stay in the build stage.
COPY --from=sqlite-build /usr/local/lib/libsqlite3.so* /usr/local/lib/
ENV LD_LIBRARY_PATH=/usr/local/lib

# Pinned Whisper model for local speech-to-text (see the build stage above).
COPY --from=sqlite-build /opt/whisper/ggml-small.bin /opt/whisper/ggml-small.bin

# Hash-locked Python dependencies: the exact resolution CI audits (includes the
# gallery-dl / yt-dlp extractors); all ship wheels, nothing needs the discarded
# compiler toolchain. Regenerate with scripts/lock_deps.py.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock

WORKDIR /app
COPY clipshelf.py clipshelf.py
COPY clipshelf/ clipshelf/
COPY deploy/ deploy/
COPY THIRD_PARTY_NOTICES.md THIRD_PARTY_NOTICES.md

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    CLIPSHELF_DATA_DIR=/data \
    CLIPSHELF_WHISPER_MODEL=/opt/whisper/ggml-small.bin

# Unprivileged default; compose.yaml overrides with the real dataset UID/GID via
# `user:`. No docker socket, no host home, nothing writable outside /data and /tmp.
USER 10001:10001
EXPOSE 8000

ARG IMAGE_VERSION=dev
ARG IMAGE_REVISION=dev
ENV CLIPSHELF_VERSION=${IMAGE_VERSION}
LABEL org.opencontainers.image.title="clipshelf" \
    org.opencontainers.image.description="Clipshelf capture/library server (Django, SQLite WAL, one worker)" \
    org.opencontainers.image.licenses="MIT AND GPL-2.0-only" \
    org.opencontainers.image.version="${IMAGE_VERSION}" \
    org.opencontainers.image.revision="${IMAGE_REVISION}" \
    org.opencontainers.image.sqlite.version="${SQLITE_VERSION}"

# Explicit operations only (default: serve). See deploy/entrypoint.py:
#   serve | worker [--once] | migrate | bootstrap | check
ENTRYPOINT ["python", "deploy/entrypoint.py"]
CMD ["serve"]
