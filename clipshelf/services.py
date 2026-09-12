"""Core domain services (rev. 3).

Every permission check and durable write for captures, jobs, findings, and
entries lives here. Bad/conflicting input raises ValidationError (request-ID
reuse conflicts use code="conflict" so the API maps them to 409);
unauthorized actions raise PermissionDenied. Store functions run short
transactions, recheck the current account/destination, never move committed
contributions, and keep the last good findings when reprocessing fails.
"""
import hashlib
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from clipshelf.lib import norm as lib_norm

from clipshelf.models import (
    Asset,
    Capture,
    Collection,
    Contribution,
    Entry,
    History,
    Job,
    Membership,
    ServerSettings,
)

MAX_TEXT_BYTES = 32768
MAX_URLS = 50
MAX_URL_LENGTH = 2048
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Settings and collections
# ---------------------------------------------------------------------------

def get_settings():
    """Singleton ServerSettings row (pk=1); instance_id survives restore."""
    row, _ = ServerSettings.objects.get_or_create(pk=1)
    return row


def personal_collection(user):
    """The user's permanent, non-transferable Personal collection."""
    existing = Collection.objects.filter(
        kind=Collection.Kind.PERSONAL, owner=user
    ).first()
    if existing is not None:
        return existing
    try:
        return Collection.objects.create(
            kind=Collection.Kind.PERSONAL, owner=user, name="Personal"
        )
    except IntegrityError:
        # Signal raced us to it; the partial unique index guarantees one.
        return Collection.objects.get(kind=Collection.Kind.PERSONAL, owner=user)


def accessible_collections(user):
    """QuerySet of collections the user may see (owned or member)."""
    return (
        Collection.objects.filter(Q(owner=user) | Q(memberships__user=user))
        .distinct()
        .order_by("kind", "name")
    )


def can_write(user, collection):
    if user is None or collection is None or not user.is_active:
        return False
    if collection.owner_id == user.id:
        return True
    if collection.kind == Collection.Kind.PERSONAL:
        return False
    return Membership.objects.filter(collection_id=collection.id, user_id=user.id).exists()


def destination_for(user, requested_id):
    """Resolve the effective capture destination. Falls back to the same
    user's Personal collection with a visible notice; never raises for an
    unavailable destination, only for malformed input."""
    requested = _requested_uuid(requested_id)
    if requested is not None:
        collection = Collection.objects.filter(pk=requested).first()
        if collection is not None and can_write(user, collection):
            return collection, ""
    else:
        default = user.default_collection
        if default is not None and can_write(user, default):
            return default, ""
    if requested is not None or user.default_collection_id:
        notice = "Selected collection is unavailable; saved to your Personal collection."
    else:
        notice = ""
    return personal_collection(user), notice


# ---------------------------------------------------------------------------
# Capture intake
# ---------------------------------------------------------------------------

def accept_capture(user, client_request_id, text, collection_id, instance_id=None, user_id=None):
    """Validate and durably accept a capture: one transaction writes the
    receipt and queued jobs. Identical retries return the existing capture
    unchanged (created=False); conflicting reuse of the request ID raises
    ValidationError(code="conflict")."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError({"text": "Text is required."}, code="invalid")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_TEXT_BYTES:
        raise ValidationError({"text": f"Text exceeds {MAX_TEXT_BYTES} UTF-8 bytes."}, code="invalid")

    current = get_settings()
    if instance_id is not None and str(instance_id).strip().lower() != str(current.instance_id):
        raise ValidationError(
            "Server identity changed; refresh server details and resend.",
            code="conflict",
        )
    if user_id is not None and str(user_id).strip().lower() != str(user.id):
        raise ValidationError(
            "Captures must be submitted by the authenticated account.",
            code="conflict",
        )
    try:
        request_id = uuid.UUID(str(client_request_id))
    except (TypeError, ValueError):
        raise ValidationError({"client_request_id": "client_request_id must be a UUID."}, code="invalid")

    urls = _extract_urls(text)
    request_hash = hashlib.sha256(encoded).hexdigest()

    existing = Capture.objects.filter(user=user, client_request_id=request_id).first()
    if existing is not None:
        return _replay(existing, request_hash)

    collection, notice = destination_for(user, collection_id)
    requested = _requested_uuid(collection_id)
    # ponytail: minimal local canonicalization (scheme/host/port/fragment);
    # switch to clipshelf.lib.norm if deeper legacy-compatible rules are needed.
    canonical_urls = list(dict.fromkeys(canonical_url(u) for u in urls))
    try:
        with transaction.atomic():
            capture = Capture.objects.create(
                user=user,
                client_request_id=request_id,
                raw_text=text,
                requested_collection_id=requested,
                collection=collection,
                request_hash=request_hash,
                notice=notice,
            )
            Job.objects.bulk_create(
                [Job(capture=capture, collection=collection, url=u) for u in canonical_urls]
            )
            History.objects.bulk_create(
                [
                    History(collection=collection, user=user, kind=History.Kind.SEEN, url=u)
                    for u in canonical_urls
                ],
                ignore_conflicts=True,
            )
    except IntegrityError:
        # Concurrent duplicate of the same (user, client_request_id).
        existing = Capture.objects.filter(user=user, client_request_id=request_id).first()
        if existing is not None:
            return _replay(existing, request_hash)
        raise
    return capture, True


def receipt(capture):
    """JSON-safe immutable receipt for an accepted capture."""
    return {
        "id": str(capture.id),
        "client_request_id": str(capture.client_request_id),
        "instance_id": str(get_settings().instance_id),
        "user_id": str(capture.user_id),
        "requested_collection_id": (
            str(capture.requested_collection_id) if capture.requested_collection_id else None
        ),
        "collection_id": str(capture.collection_id),
        "received_at": capture.received_at.isoformat(),
        "notice": capture.notice,
    }


# ---------------------------------------------------------------------------
# Entry serialization and removal
# ---------------------------------------------------------------------------

def serialize_entry(entry, user):
    """Flat API entry merged from the contributions REMAINING in this
    collection. Private data of removed contributions never appears."""
    contributions = list(entry.contributions.select_related("user").order_by("created_at", "id"))
    title = desc = text = install = ""
    categories, tags, sources = set(), set(), []
    source_urls = set()
    contributors = []
    interpreted = False
    user_contributed = False
    for contribution in contributions:
        data = contribution.data or {}
        title = title or _str(data.get("title"))
        desc = desc or _str(data.get("desc"))
        text = text or _str(data.get("text"))
        install = install or _str(data.get("install"))
        categories.update(v for v in data.get("categories") or [] if isinstance(v, str))
        tags.update(v for v in data.get("tags") or [] if isinstance(v, str))
        for item in data.get("sources") or []:
            url = _str(item.get("url")) if isinstance(item, dict) else _str(item)
            # Skip, never raise: malformed/legacy data must not 500 entry reads.
            if not url or url in source_urls:
                continue
            try:
                _require_url(url)
            except ValidationError:
                continue
            source_urls.add(url)
            sources.append({"url": url, "title": _str(item.get("title")) if isinstance(item, dict) else ""})
        if contribution.user_id == user.id:
            user_contributed = True
        contributors.append({"id": str(contribution.user_id), "email": contribution.user.email})
    return {
        "id": str(entry.id),
        "kind": entry.kind,
        "url": entry.key if entry.kind == Entry.Kind.LINK else "",
        "title": title,
        "desc": desc,
        "text": text,
        "cat": sorted(categories),
        "tags": sorted(tags),
        "install": install,  # copy-only data; never executed
        "sources": sources,
        "found": entry.created_at.isoformat(),
        "interpreted": interpreted,
        "contributors": contributors,
        "can_remove": user_contributed or entry.collection.owner_id == user.id,
    }


def remove_entry(user, entry):
    """Remove the caller's contributions; a collection owner may remove the
    whole entry. Records a collection+user-local tombstone so worker/import/
    findings never resurrect the removed key. Entry disappears only when no
    contributions remain; retained asset bytes are never deleted here."""
    is_owner = entry.collection.owner_id == user.id
    if is_owner:
        targets = entry.contributions.all()
    else:
        targets = entry.contributions.filter(user=user)
        if not targets.exists():
            raise PermissionDenied("You can only remove your own contributions.")
    with transaction.atomic():
        targets.delete()
        History.objects.update_or_create(
            collection=entry.collection,
            user=user,
            kind=History.Kind.REMOVED,
            url=entry.key,
            defaults={"data": {}},
        )
        if not entry.contributions.exists():
            entry.delete()


# ---------------------------------------------------------------------------
# Worker result storage
# ---------------------------------------------------------------------------

def store_source(job, source):
    """Persist an acquisition result: recheck account/destination, sync Asset
    rows for files the worker already published atomically under DATA_DIR,
    then store the job source and the collection-scoped entry/contribution.
    Never moves a committed contribution to another collection."""
    if not isinstance(source, dict):
        raise ValidationError({"source": "Source must be an object."}, code="invalid")
    url = _str(source.get("url"))
    _require_url(url)
    status = source.get("acquisition")
    if status not in Job.AcquisitionStatus.values:
        raise ValidationError({"acquisition": "Invalid acquisition status."}, code="invalid")
    warnings = _string_list(source.get("warnings"), 100, 500)
    links = _link_list(source.get("links"), 200)
    prepared_assets = _prepare_assets(source.get("assets") or [])
    caller_job = job  # the locked row below is a separate instance

    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        user = job.capture.user
        if not user.is_active:
            raise PermissionDenied("Account is disabled.")
        if not can_write(user, job.collection):
            if Contribution.objects.filter(job=job).exists():
                # Committed work stays where it is; never move it on fallback.
                raise PermissionDenied("Collection access was lost for a committed contribution.")
            job.collection = personal_collection(user)
            warnings.append("Selected collection is unavailable; results saved to your Personal collection.")
        canonical = canonical_url(url)
        tombstoned = _tombstoned(job.collection_id, user.id, canonical)
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}

        job.final_url = _str(source.get("original_url")) or url
        job.acquisition = status
        job.error = ""
        job.source = {
            "url": url,
            "original_url": job.final_url,
            "title": _str(source.get("title"))[:500],
            "desc": _str(source.get("desc"))[:2000],
            "text": _str(source.get("text"))[:50000],
            "links": links,
            "metadata": metadata,
            "acquisition": status,
            "warnings": warnings,
            "assets": [
                {
                    "path": a["path"],  # relative to DATA_DIR only; no absolute paths stored
                    "kind": a["kind"],
                    "content_type": a["content_type"],
                    "position": a["position"],
                    "size": a["size"],
                    "sha256": a["sha256"],
                }
                for a in prepared_assets
            ],
        }
        Asset.objects.filter(job=job).delete()
        Asset.objects.bulk_create(
            [
                Asset(
                    job=job,
                    collection=job.collection,  # snapshot authorization
                    path=a["path"],
                    kind=a["kind"],
                    content_type=a["content_type"],
                    size=a["size"],
                    sha256=a["sha256"],
                    position=a["position"],
                )
                for a in prepared_assets
            ]
        )
        if tombstoned:
            warnings.append("URL was removed from this collection; not re-adding it.")
        else:
            _contribute_link(
                job,
                canonical,
                {
                    "title": _str(source.get("title"))[:500],
                    "desc": _str(source.get("desc"))[:2000],
                    "text": _str(source.get("text"))[:50000],
                    "sources": links,
                    "tags": _string_list(metadata.get("tags"), 50, 100),
                    "categories": _string_list(metadata.get("categories"), 50, 100),
                },
            )
        job.warnings = list(dict.fromkeys(list(job.warnings or []) + warnings))[:100]
        job.save()
    caller_job.refresh_from_db()


def store_findings(job, findings):
    """Atomically publish validated findings and job completion. Called only
    on successful interpretation; a failing reprocess never touches the last
    good findings (the worker marks blocked/error itself)."""
    validated = _validate_findings(job, findings)
    caller_job = job  # the locked row below is a separate instance

    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job.pk)
        user = job.capture.user
        if not user.is_active:
            raise PermissionDenied("Account is disabled.")
        if not can_write(user, job.collection):
            # Access lost after acquisition: keep the last good findings, do
            # not write into a collection the user can no longer write.
            job.interpretation = Job.InterpretationStatus.BLOCKED
            job.error = "Collection access was lost; previous findings were kept."
            job.save()
            caller_job.refresh_from_db()
            return False
        canonical = canonical_url(job.url)
        warnings = list(job.warnings or [])
        if _tombstoned(job.collection_id, user.id, canonical):
            warnings.append("URL was removed from this collection; findings were not attached.")
        else:
            _contribute_link(
                job,
                canonical,
                {
                    "findings": validated,
                    "interpreted_at": timezone.now().isoformat(),
                    "categories": validated["categories"],
                },
            )
            for prompt in validated["prompts"]:
                digest = hashlib.sha256(" ".join(prompt.split()).encode("utf-8")).hexdigest()
                if _tombstoned(job.collection_id, user.id, digest):
                    continue
                entry, _ = Entry.objects.get_or_create(
                    collection=job.collection, kind=Entry.Kind.PROMPT, key=digest
                )
                Contribution.objects.update_or_create(
                    entry=entry,
                    user=user,
                    origin="capture",
                    defaults={"capture": job.capture, "job": job, "data": {"text": prompt, "source_url": job.url}},
                )
        job.findings = validated
        job.interpretation = Job.InterpretationStatus.COMPLETE
        job.state = Job.State.DONE
        job.error = ""
        job.warnings = list(dict.fromkeys(warnings + validated["warnings"]))[:100]
        job.save()
    caller_job.refresh_from_db()
    return True


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def canonical_url(url):
    """Canonical entry key: the same rules the capture parser applies.

    lib.norm drops fragments, tracking params and the trailing slash, so a
    findings link and the captured URL collapse onto one entry. The fallback
    below only runs for strings lib.norm rejects.
    """
    canonical = lib_norm(url)
    if canonical:
        return canonical
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if not host:
            return url.strip()
        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"  # bare IPv6 literal
        default_port = {"http": 80, "https": 443}.get(scheme)
        if parts.port and parts.port != default_port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    except ValueError:
        return url.strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _replay(capture, request_hash):
    if capture.request_hash != request_hash:
        raise ValidationError(
            "This client request ID was already used for different content.",
            code="conflict",
        )
    return capture, False


def _requested_uuid(value):
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ValidationError({"collection_id": "collection_id must be a UUID."}, code="invalid")


def _extract_urls(text):
    candidates = [match.rstrip(".,;:!?)]}\"'") for match in URL_PATTERN.findall(text)]
    validated = []
    for url in candidates:
        if not url or len(url) > MAX_URL_LENGTH:
            raise ValidationError({"text": "A URL in the text is malformed or too long."}, code="invalid")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValidationError({"text": "Text must contain at least one valid http(s) URL."}, code="invalid")
        validated.append(url)
    if not validated:
        raise ValidationError({"text": "Text must contain at least one valid http(s) URL."}, code="invalid")
    if len(validated) > MAX_URLS:
        raise ValidationError({"text": f"Text contains more than {MAX_URLS} URLs."}, code="invalid")
    return validated


def _require_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValidationError({"url": "Invalid URL."}, code="invalid")
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > MAX_URL_LENGTH:
        raise ValidationError({"url": "Invalid http(s) URL."}, code="invalid")


def _link_list(value, limit):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError({"links": "Expected a list."}, code="invalid")
    links = []
    for item in value[:limit]:
        if isinstance(item, dict):
            url = _str(item.get("url"))
            _require_url(url)
            links.append({"url": url, "title": _str(item.get("title"))[:500]})
        elif isinstance(item, str):
            _require_url(item)
            links.append({"url": item, "title": ""})
        else:
            raise ValidationError({"links": "Expected URL objects."}, code="invalid")
    return links


def _string_list(value, limit, max_len):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError({"list": "Expected a list of strings."}, code="invalid")
    return [_str(v)[:max_len] for v in value[:limit] if _str(v)]


def _str(value):
    return "" if value is None else str(value).strip()


def _validate_findings(job, findings):
    if not isinstance(findings, dict):
        raise ValidationError({"findings": "Findings must be an object."}, code="invalid")
    source_url = _str(findings.get("source"))
    if source_url not in {job.url, job.final_url, canonical_url(job.url)}:
        raise ValidationError({"source": "Findings do not match this job's source."}, code="invalid")
    return {
        "source": source_url,
        "summary": _str(findings.get("summary"))[:8000],
        "repos": _link_list(findings.get("repos"), 50),
        "prompts": _string_list(findings.get("prompts"), 100, 20000),
        "links": _link_list(findings.get("links"), 200),
        "categories": _string_list(findings.get("categories"), 100, 100),
        "installs": _string_list(findings.get("installs"), 50, 2000),
        "warnings": _string_list(findings.get("warnings"), 100, 500),
    }


def _prepare_assets(assets):
    """Validate worker-published files BEFORE the short write transaction:
    resolve each path strictly under DATA_DIR (absolute or relative, no
    traversal), require the file to exist, and hash/measure it there."""
    if not isinstance(assets, list):
        raise ValidationError({"assets": "Expected a list."}, code="invalid")
    data_dir = Path(settings.DATA_DIR).resolve()
    prepared = []
    for index, asset in enumerate(assets[:500]):
        if not isinstance(asset, dict):
            raise ValidationError({"assets": "Expected asset objects."}, code="invalid")
        raw = _str(asset.get("path"))
        if not raw:
            raise ValidationError({"assets": "Asset path is required."}, code="invalid")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (data_dir / candidate).resolve()
        try:
            relative = resolved.relative_to(data_dir)
        except ValueError:
            raise ValidationError({"assets": "Asset path escapes the data directory."}, code="invalid")
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
                "path": relative.as_posix(),
                "kind": kind,
                "content_type": _str(asset.get("content_type"))[:255],
                "position": position,
                "size": resolved.stat().st_size,
                "sha256": _file_sha256(resolved),
            }
        )
    return prepared


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contribute_link(job, canonical, data):
    """Create or merge the caller's capture contribution. Merge (not replace)
    so a good earlier `findings` blob survives a later failed reprocess."""
    entry, _ = Entry.objects.get_or_create(
        collection=job.collection, kind=Entry.Kind.LINK, key=canonical
    )
    existing = Contribution.objects.filter(
        entry=entry, user=job.capture.user, origin="capture"
    ).first()
    if existing is None:
        Contribution.objects.create(
            entry=entry, user=job.capture.user, capture=job.capture, job=job, data=data
        )
        return
    merged = dict(existing.data or {})
    merged.update(data)
    existing.data = merged
    existing.capture = job.capture
    existing.job = job
    existing.save()


def _tombstoned(collection_id, user_id, key):
    return History.objects.filter(
        collection_id=collection_id, user_id=user_id, kind=History.Kind.REMOVED, url=key
    ).exists()
