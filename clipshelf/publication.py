"""Publication policy: one owner for Entry identity, tombstones, account
rechecks, and the capture-route contribution merge.

Every findings producer (worker store_source/store_findings, import commit,
extracted merge) routes identity and tombstone decisions through here so a
removed entry can never resurrect on another route and the same link/prompt
lands on one Entry per collection.

Identity: links key on the canonical URL; prompts key on SHA-256 of
whitespace-normalized text, with the legacy SHA-1(lowercase-normalized)[:16]
scheme recognized wherever entries or tombstones already shipped under it.
ponytail: no destructive re-key migration — historical SHA-1 entries and
hash-only tombstones are reused in place; new prompts always get SHA-256.
"""
import hashlib
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import PermissionDenied

from clipshelf import lib
from clipshelf.models import Contribution, Entry, History


def link_key(url):
    """Canonical link entry key: the same rules the capture parser applies.

    lib.norm drops fragments, tracking params and the trailing slash, so a
    findings link and the captured URL collapse onto one entry. The fallback
    below only runs for strings lib.norm rejects.
    """
    canonical = lib.norm(url)
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


def prompt_key(text):
    """Legacy prompt identity: SHA-1(lowercase whitespace-normalized)[:16].
    Recognized for existing entries/tombstones; never minted for new prompts."""
    return hashlib.sha1(" ".join(str(text).split()).lower().encode()).hexdigest()[:16]


def prompt_digest(text):
    """Current prompt identity for NEW prompts: SHA-256(whitespace-normalized)."""
    return hashlib.sha256(" ".join(str(text).split()).encode("utf-8")).hexdigest()


def tombstoned(collection_id, user_id, *keys):
    """True when this user removed any of these identities in this collection."""
    keys = [key for key in keys if key]
    return bool(keys) and History.objects.filter(
        collection_id=collection_id, user_id=user_id,
        kind=History.Kind.REMOVED, url__in=keys
    ).exists()


def job_account(job):
    """Recheck the account inside the write transaction: active or no write."""
    user = job.capture.user
    if not user.is_active:
        raise PermissionDenied("Account is disabled.")
    return user


def prompt_entry(collection, user, text):
    """Deterministic prompt Entry for this text, or None when tombstoned.

    Reuses an entry already shipped under either recognized identity so the
    same text stays one Entry per collection; a new prompt gets the SHA-256
    key. A tombstone under either key blocks publication on every route.
    """
    legacy, current = prompt_key(text), prompt_digest(text)
    if tombstoned(collection.id, user.id, legacy, current):
        return None
    key = current
    existing = Entry.objects.filter(
        collection=collection, kind=Entry.Kind.PROMPT, key__in={legacy, current})
    if not existing.filter(key=key).exists() and existing.filter(key=legacy).exists():
        key = legacy  # shipped under the legacy scheme: keep its identity
    entry, _ = Entry.objects.get_or_create(
        collection=collection, kind=Entry.Kind.PROMPT, key=key)
    return entry


def publish_prompt(*, collection, user, text, data, origin, capture=None, job=None):
    """Create/replace this user's contribution on the prompt entry. Returns
    False when the text was removed from this collection (never resurrects)."""
    entry = prompt_entry(collection, user, text)
    if entry is None:
        return False
    Contribution.objects.update_or_create(
        entry=entry, user=user, origin=origin,
        defaults={"capture": capture, "job": job, "data": data})
    return True


def contribute_link(*, collection, user, capture, job, key, data):
    """Create or merge the caller's capture contribution. Merge (not replace)
    so a good earlier `findings` blob survives a later failed reprocess."""
    entry, _ = Entry.objects.get_or_create(
        collection=collection, kind=Entry.Kind.LINK, key=key)
    existing = Contribution.objects.filter(
        entry=entry, user=user, origin="capture"
    ).first()
    if existing is None:
        Contribution.objects.create(
            entry=entry, user=user, capture=capture, job=job, data=data)
        return
    merged = dict(existing.data or {})
    merged.update(data)
    existing.data = merged
    existing.capture = capture
    existing.job = job
    existing.save()
