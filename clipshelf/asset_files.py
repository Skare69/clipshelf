"""Retained-file custody: the single module that converts between stored
(DATA_DIR-relative) asset paths and real filesystem locations, and that moves
or copies staged bytes into their published locations.

Stored `Asset.path` values are DATA_DIR-relative POSIX strings, never served
raw and never stored absolute. Publication prep (publish) runs BEFORE any DB
reference exists; hashing/validation prep (prepare) runs before the short
write transaction, so a crash can never leave a committed row pointing at
partial or missing bytes. This helper grants no authorization; callers keep
their own collection/ownership checks.
"""
import hashlib
import os
import shutil
import uuid
from pathlib import Path, PureWindowsPath

from django.conf import settings
from django.core.exceptions import ValidationError

from clipshelf.models import Asset

MAX_ASSETS = 500


def resolve_path(path, root=None):
    """Resolve a stored asset path to an absolute Path inside `root`.

    `root` defaults to settings.DATA_DIR. Backslashes are normalized to
    slashes first; the root and candidate are resolved through symlinks so an
    in-tree symlink pointing outside is rejected. Absolute paths INSIDE root
    are accepted. A Windows drive/UNC path on POSIX is rejected outright —
    it must never pass as harmless relative text. File existence is NOT
    required: material building historically tolerates missing retained media
    for interpreter warning handling; file-opening callers check themselves.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValidationError("asset path must be a non-empty string", code="invalid")
    root = Path(settings.DATA_DIR if root is None else root).resolve()
    text = path.replace("\\", "/")
    windows = PureWindowsPath(text)
    if windows.drive:
        if os.name != "nt":
            raise ValidationError(
                "windows drive or UNC path is not a valid data-directory path",
                code="invalid",
            )
        if not windows.is_absolute():
            raise ValidationError("drive-relative asset path is ambiguous", code="invalid")
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValidationError("asset path escapes the data directory", code="invalid")
    return resolved


def publish(source, staging, *, move):
    """Move or copy `source['assets']` from their staging location into a
    fresh unique directory under DATA_DIR/assets.

    Every staged file is validated strictly inside `staging` and existing
    before anything is published. `move=True` is for acquired files (the
    staging copy is consumed); `move=False` is for imports whose cache files
    may feed multiple entries. Copies land via a temporary name + os.replace
    so a file is never observable partial at its final path. Never overwrites
    prior publications: the destination directory is fresh per call. Sets
    `source['assets']` to copies carrying published paths and returns that
    list; empty assets publishes nothing and returns [].
    """
    assets = source.get("assets") if isinstance(source, dict) else None
    if not assets:
        return []
    staging_root = Path(staging).resolve()
    staged = []
    for asset in assets:
        src = resolve_path(asset["path"], root=staging_root)
        if not src.is_file():
            raise ValidationError(
                f"staged asset is missing: {src.name}", code="invalid"
            )
        staged.append((asset, src))
    root = Path(settings.DATA_DIR) / "assets" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    published = []
    for index, (asset, src) in enumerate(staged):
        dest = root / f"{index}-{src.name}"
        if move:
            os.replace(src, dest)  # atomic: file appears complete at final path
        else:
            part = dest.with_name(f".{dest.name}.part")
            shutil.copy2(src, part)
            os.replace(part, dest)
        published.append({**asset, "path": str(dest)})
    source["assets"] = published
    return published


def prepare(assets):
    """Validate published files BEFORE the short write transaction: resolve
    each path under DATA_DIR via resolve_path, require the file to exist, and
    hash/measure it there. Returns dicts with path (DATA_DIR-relative POSIX),
    kind, content_type, position, size, sha256 — the exact shape stored on
    Asset rows and in job source blobs."""
    if not isinstance(assets, list):
        raise ValidationError({"assets": "Expected a list."}, code="invalid")
    data_dir = Path(settings.DATA_DIR).resolve()
    prepared = []
    for index, asset in enumerate(assets[:MAX_ASSETS]):
        if not isinstance(asset, dict):
            raise ValidationError({"assets": "Expected asset objects."}, code="invalid")
        raw = _str(asset.get("path"))
        if not raw:
            raise ValidationError({"assets": "Asset path is required."}, code="invalid")
        resolved = resolve_path(raw)
        if not resolved.is_file():
            raise ValidationError(
                {"assets": "Asset file must be published before storing."}, code="invalid"
            )
        kind = asset.get("kind")
        if kind not in Asset.Kind.values:
            raise ValidationError({"assets": "Invalid asset kind."}, code="invalid")
        try:
            position = max(0, int(asset.get("position") or index))
        except (TypeError, ValueError):
            raise ValidationError({"assets": "Invalid asset position."}, code="invalid")
        prepared.append(
            {
                "path": resolved.relative_to(data_dir).as_posix(),
                "kind": kind,
                "content_type": _str(asset.get("content_type"))[:255],
                "position": position,
                "size": resolved.stat().st_size,
                "sha256": _file_sha256(resolved),
            }
        )
    return prepared


def _str(value):
    return "" if value is None else str(value).strip()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
