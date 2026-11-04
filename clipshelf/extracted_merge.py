"""Extracted-merge flow: merge_findings merges skill findings (the SKILL.md
schema: categories/installs are objects keyed by repo) into the Personal
collection for add_extracted."""
from django.db import transaction
from django.utils import timezone

from clipshelf import judgment, lib, models, publication, services


# ------------------------------------------------------- extracted-merge flow

def merge_findings(user, skill_findings, origin="extracted"):
    """Merge skill findings (the SKILL.md schema: categories/installs are
    objects keyed by repo) into the user's Personal collection with legacy
    add_extracted semantics (longer text wins, tags/sources union)."""
    personal = services.personal_collection(user)
    stats = {"links": 0, "prompts": 0, "updated": 0}
    now = timezone.now()
    # One screening pass for every non-structural link, outside the transaction.
    # Structural tags (github, terminal hosts via lib.tag_for) stay deterministic
    # and never hit the API; keyword/untagged links go to judgment.tag_links.
    meta = {}
    candidates = []
    for fi, finding in enumerate(skill_findings):
        for li, link in enumerate(finding.get("links") or []):
            url = lib.norm(str(link.get("url", "")))
            title = " ".join(str(link.get("title", "")).split())
            base = lib.tag_for(url, title)
            meta[(fi, li)] = (url, title, base or "page")
            if url and (base is None or base in lib.KEYWORDS):
                candidates.append((fi, li, url, title))
    tag_map = {}
    key = services.screening_key() if candidates else None
    if candidates and judgment.available(key):
        try:
            results = judgment.tag_links(
                [{"url": u, "title": t} for _, _, u, t in candidates], api_key=key)
        except judgment.JudgmentError:
            results = None  # fail-open: keep today's lib.tag_for behavior
        if results:
            tag_map = {(fi, li): (r["tag"] if r["keep"] >= judgment.REVIEW_STEER
                                  else "page")
                       for (fi, li, _, _), r in zip(candidates, results)}
    with transaction.atomic():
        for fi, finding in enumerate(skill_findings):
            source = lib.norm(str(finding.get("source", "")))
            cats = finding.get("categories") or {}
            installs = finding.get("installs") or {}
            if source:
                entry = _merge_entry(personal, user, source)
                if entry and _merge_own(entry, user, origin, summary=finding.get("summary"),
                                        sources=[source], now=now):
                    stats["updated"] += 1
            for repo in finding.get("repos") or []:
                entry = _merge_entry(personal, user, lib.repo_url(repo))
                if entry and _merge_own(entry, user, origin,
                                        sources=[source] if source else [],
                                        tags=["github"], cat=cats.get(repo),
                                        install=installs.get(repo), now=now):
                    stats["links"] += 1
            for li, link in enumerate(finding.get("links") or []):
                url, title, tag = meta[(fi, li)]
                entry = _merge_entry(personal, user, url)
                if entry and _merge_own(entry, user, origin, title=title,
                                        sources=[source] if source else [],
                                        tags=[tag_map.get((fi, li)) or tag],
                                        now=now):
                    stats["links"] += 1
            for prompt in finding.get("prompts") or []:
                text = (prompt.get("text") or "").strip()
                if not text:
                    continue
                entry = publication.prompt_entry(personal, user, text)
                if entry and _merge_own(entry, user, origin, prompt_text=text,
                                        title=prompt.get("title", ""), cat=prompt.get("cat", ""),
                                        sources=[source] if source else [], now=now):
                    stats["prompts"] += 1
    return stats


def _merge_entry(personal, user, url):
    """Personal-collection link entry for an extracted URL, or None when the
    URL is empty or this user removed it: a merge never resurrects it."""
    if not url:
        return None
    key = publication.link_key(url)
    if publication.tombstoned(personal.id, user.id, *publication.link_keys(key)):
        return None
    entry, _ = publication.link_entry(personal, key)
    return entry


def _merge_own(entry, user, origin, now, title="", summary=None, prompt_text=None,
               sources=None, tags=None, cat=None, install=None):
    """Create/update this user's contribution with legacy merge semantics.
    Returns True when a contribution was created or materially changed."""
    contribution, created = models.Contribution.objects.get_or_create(
        entry=entry, user=user, origin=origin,
        defaults={"data": {"title": "", "desc": "", "sources": [], "tags": []}})
    data = dict(contribution.data or {})
    changed = created
    if prompt_text and not data.get("text"):
        data["text"] = prompt_text
        changed = True
    if title and len(title) > len(data.get("title", "")):
        data["title"] = title
        changed = True
    if summary and len(summary) > len(data.get("desc", "")):
        data["desc"] = summary
        changed = True
    merged_sources = sorted(set(data.get("sources") or []) | {s for s in (sources or []) if s})
    merged_tags = sorted(set(data.get("tags") or []) | {t for t in (tags or []) if t})
    if merged_sources != data.get("sources"):
        data["sources"] = merged_sources
        changed = True
    if merged_tags != data.get("tags"):
        data["tags"] = merged_tags
        changed = True
    if cat and not data.get("cat"):
        data["cat"] = cat
        changed = True
    if install and not data.get("install"):
        data["install"] = install
        changed = True
    if changed:
        data["interpreted"] = now.isoformat()
        contribution.data = data
        contribution.save(update_fields=["data"])
    return changed
