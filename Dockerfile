# syntax=docker/dockerfile:1
# Clipshelf server image: one image, two roles (web serve / background worker).
# Runs unprivileged; the operator chooses the actual runtime UID/GID in compose.yaml.
#
# Pin the base image digest after the first verified build:
#   docker image inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
FROM python:3.13-slim

# Pinned SQLite with the WAL-reset corruption fix (>= 3.51.3, per
# https://www.sqlite.org/wal.html). The distro build inside a python image tag is
# NOT assumed fixed, so we build a known-good SQLite from the pinned release
# tarball and put it first on the loader path. Verify/re-pin when bumping:
#   https://www.sqlite.org/download.html (sha256 quoted there and re-checked in build)
ARG SQLITE_VERSION=3.53.1
ARG SQLITE_TARBALL=sqlite-autoconf-3530100.tar.gz
ARG SQLITE_URL=https://sqlite.org/2026/sqlite-autoconf-3530100.tar.gz
ARG SQLITE_SHA256=83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f

# ffmpeg is the only OS package the worker needs (fixed version within the Debian
# release; invocations stay fixed-arg in clipshelf.sources). gcc/make/libc6-dev are
# build-only for SQLite and are not needed at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*

# Download, checksum-verify, build, and install the pinned SQLite into /usr/local.
# Refuses to proceed on any hash mismatch.
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
    make install; \
    ldconfig; \
    rm -rf /tmp/src
# Python's _sqlite3 extension links soname libsqlite3.so.0; the freshly installed
# shared library satisfies it at import time.
ENV LD_LIBRARY_PATH=/usr/local/lib

# Pinned Python dependencies (owned by clipshelf requirements, includes the
# gallery-dl / yt-dlp extractors).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY clipshelf.py clipshelf.py
COPY clipshelf/ clipshelf/
COPY deploy/ deploy/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    CLIPSHELF_DATA_DIR=/data

# Unprivileged default; compose.yaml overrides with the real dataset UID/GID via
# `user:`. No docker socket, no host home, nothing writable outside /data and /tmp.
USER 10001:10001
EXPOSE 8000

ARG IMAGE_VERSION=dev
ARG IMAGE_REVISION=dev
ENV CLIPSHELF_VERSION=${IMAGE_VERSION}
LABEL org.opencontainers.image.title="clipshelf" \
    org.opencontainers.image.description="Clipshelf capture/library server (Django, SQLite WAL, one worker)" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${IMAGE_VERSION}" \
    org.opencontainers.image.revision="${IMAGE_REVISION}" \
    org.opencontainers.image.sqlite.version="${SQLITE_VERSION}"

# Explicit operations only (default: serve). See deploy/entrypoint.py:
#   serve | worker [--once] | migrate | bootstrap | check
ENTRYPOINT ["python", "deploy/entrypoint.py"]
CMD ["serve"]
