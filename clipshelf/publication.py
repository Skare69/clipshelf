"""Publication policy: one owner for Entry identity, tombstones, and the
capture-route contribution merge.

Every findings producer (worker store_source/store_findings, import commit,
extracted merge) routes identity and tombstone decisions through here so a
removed entry can never resurrect on another route and the same link/prompt
lands on one Entry per collection.

Identity: links key on the canonical URL (GitHub owner/repo folds, deep
ref/file path case is preserved) with the legacy all-lowercase path
recognized wherever entries or tombstones already shipped under it; prompts
key on SHA-256 of whitespace-normalized text, with the legacy
SHA-1(lowercase-normalized)[:16] scheme recognized the same way.
ponytail: no destructive re-key migration — historical SHA-1 entries,
hash-only tombstones, and legacy lowercase link keys are reused in place;
new prompts always get SHA-256, new link entries the case-preserving key.
"""
import hashlib
from urllib.parse import urlsplit, urlunsplit

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


def legacy_link_key(url):
    """Legacy link identity: the all-lowercase GitHub path the pre
    case-preserving norm minted (owner/repo folding was already the same;
    deep ref/file case was flattened). Recognized wherever entries or
    tombstones already shipped under it; never minted for new entries.
    Non-GitHub URLs never had a case rule, so the current key is returned."""
    key = link_key(url)
    parts = urlsplit(key)
    if (parts.hostname or "") not in ("github.com", "gist.github.com"):
        return key
    return urlunsplit((parts.scheme, parts.netloc, parts.path.lower(),
                       parts.query, ""))


def link_keys(url):
    """Every recognized Entry/tombstone identity for one link: the current
    case-preserving key plus its legacy all-lowercase form. Tombstone and
    dedup lookups accept the whole set; nothing stored is ever rewritten.
    Any History identity comparison (alias rows included) must use this set."""
    key = link_key(url)
    return {key, legacy_link_key(key)}


def link_entry(collection, key):
    """Variant-aware link Entry: reuses an entry shipped under the legacy
    all-lowercase identity, keeping its stored key, so the case-preserving
    key never duplicates it. New entries always get `key`."""
    legacy = legacy_link_key(key)
    if legacy != key:
        existing = Entry.objects.filter(
            collection=collection, kind=Entry.Kind.LINK, key__in=(key, legacy))
        if not existing.filter(key=key).exists():
            legacy_entry = existing.filter(key=legacy).first()
            if legacy_entry is not None:
                return legacy_entry, False
    return Entry.objects.get_or_create(
        collection=collection, kind=Entry.Kind.LINK, key=key)


def prompt_key(text):
    """Legacy prompt identity: SHA-1(lowercase whitespace-normalized)[:16].
    Recognized for existing entries/tombstones; never minted for new prompts."""
    return hashlib.sha1(" ".join(str(text).split()).lower().encode()).hexdigest()[:16]


def prompt_digest(text):
    """Current prompt identity for NEW prompts: SHA-256(whitespace-normalized)."""
    return hashlib.sha256(" ".join(str(text).split()).encode("utf-8")).hexdigest()


def tombstoned(collection_id, user_id, *keys):
    """True when this user removed any identity or alias in this collection."""
    keys = {k for key in keys if key for k in link_keys(key)}
    if not keys:
        return False
    alias_rows = History.objects.filter(
        collection_id=collection_id, user_id=user_id, kind=History.Kind.ALIAS)
    keys.update(set(alias_rows.filter(
        url__in=keys).values_list("target_url", flat=True)))
    keys.update(set(alias_rows.filter(
        target_url__in=keys).values_list("url", flat=True)))
    return History.objects.filter(
        collection_id=collection_id, user_id=user_id,
        kind=History.Kind.REMOVED, url__in=keys
    ).exists()


def record_removal(*, collection, user_ids, key):
    """Tombstone key for these users in this collection. Owner moderation
    records one row per contributor whose contribution was removed, so no
    removed contributor's pending job can resurrect the entry."""
    for user_id in set(user_ids):
        History.objects.update_or_create(
            collection=collection, user_id=user_id,
            kind=History.Kind.REMOVED, url=key, defaults={"data": {}})


def record_import_history(*, collection, user, seen, removed):
    """Persist each legacy URL and its canonical alias in scoped history."""
    rows = {}
    for kind, urls in ((History.Kind.SEEN, seen), (History.Kind.REMOVED, removed)):
        for url in urls:
            key = link_key(url)
            if not key:
                continue
            for identity in {url, key}:
                rows[(kind, identity)] = History(
                    collection=collection, user=user, kind=kind, url=identity)
            if url != key:
                rows[(History.Kind.ALIAS, url)] = History(
                    collection=collection, user=user, kind=History.Kind.ALIAS,
                    url=url, target_url=key)
    History.objects.bulk_create(rows.values(), ignore_conflicts=True)


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
    entry, _ = link_entry(collection, key)
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
