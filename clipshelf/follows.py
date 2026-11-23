"""Followed creator sources: URL validation, creation, and one check cycle.

A follow lists its TikTok profile on a schedule, queues only new posts
through the ordinary capture path, and records them in the SeenPost
ledger. The ledger row is the queue gate: the unique constraint decides
who wins a concurrent listing, and the row commits in the same
transaction as its capture, so a failed capture never consumes the
marker. The listing itself runs strictly outside any transaction.
"""
import re
import uuid
import urllib.parse
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from clipshelf import acquisition, lib, services
from clipshelf.lib import MAX_URL_LENGTH
from clipshelf.models import FollowedSource, Job, SeenPost

# Stable duplicate marker; views map it to 409.
DUPLICATE = "duplicate follow"


def validate_profile_url(url):
    """Canonical TikTok profile URL, or ValueError. Only tiktok.com hosts
    (lib.is_tiktok) whose path is exactly /@handle count, and only handles
    norm would leave alone: norm strips whole-URL whitespace and trailing
    punctuation before it splits the path, so a fragment-shielded tail
    ("…/@a. .#f") survives one norm but not a re-validation, and such a
    canonical form could never come back as itself. The canonical form pins
    the host, drops the query and lowercases the handle, so every www,
    bare-domain and case spelling of one profile is one follow. Uniqueness
    uses the returned form."""
    if not isinstance(url, str):
        raise ValueError("profile URL required")
    normalized = lib.norm(url)
    if not normalized or not lib.is_tiktok(normalized):
        raise ValueError("only tiktok.com profile URLs are supported")
    parts = urllib.parse.urlsplit(normalized)
    if (not re.fullmatch(r"/@[^/@]+", parts.path)
            or parts.path[-1].isspace() or parts.path[-1] in ".,;!"):
        raise ValueError("URL must point at a profile page (/@handle)")
    return urllib.parse.urlunsplit(
        ("https", "www.tiktok.com", parts.path.lower(), "", ""))


def create_follow(user, url, tag, full=False):
    """Create one follow for a user. Tag must be 1-100 characters after
    stripping; a duplicate user/profile row raises ValueError(DUPLICATE)."""
    profile_url = validate_profile_url(url)
    tag = str(tag or "").strip()
    if not tag or len(tag) > 100:
        raise ValueError("tag must be 1-100 characters")
    try:
        return FollowedSource.objects.create(
            user=user, platform=FollowedSource.Platform.TIKTOK,
            profile_url=profile_url, tag=tag, full_backfill=bool(full),
        )
    except IntegrityError:
        raise ValueError(DUPLICATE)


def follow_json(source):
    """JSON-safe follow row for the API."""
    return {
        "id": str(source.id),
        "platform": source.platform,
        "profile_url": source.profile_url,
        "tag": source.tag,
        "enabled": source.enabled,
        "full_backfill": source.full_backfill,
        "last_check": source.last_check.isoformat() if source.last_check else None,
        "next_check": source.next_check.isoformat() if source.next_check else None,
        "last_error": source.last_error,
        "created_at": source.created_at.isoformat(),
    }


def check_source(source, say=None):
    """One listing cycle for one followed source. Listing and capture
    failures write last_error and move next_check out by the backoff; success
    marks every listed post seen, queues the new candidates, and reschedules.
    Never raises for a listing or capture failure; a source deleted mid-check
    returns early."""
    try:
        return _check_source(source, say)
    except FollowedSource.DoesNotExist:
        # Unfollowed between claim and check: nothing left to update.
        return "missing"


def _fail_cycle(source, say, exc, event):
    """One failed cycle (listing or capture): last_error set, failures
    counted, next_check moved out by the backoff, last_check untouched. The
    write runs in its own transaction, so nothing from the failed cycle
    leaks into it. A message-less exception still names itself, because an
    empty last_error reads as healthy on the source row."""
    message = str(exc)[:500] or type(exc).__name__
    with transaction.atomic():
        source.last_error = message
        source.failures += 1
        source.next_check = timezone.now() + Job.backoff(source.failures)
        source.save(update_fields=["last_error", "failures", "next_check"])
    if say:
        say(f"follow {source.id}: {event} ({message})")


def _check_source(source, say):
    try:
        posts = acquisition.list_profile_posts(source.profile_url)
    except acquisition.ListingError as exc:
        _fail_cycle(source, say, exc, "listing failed")
        return "error"

    settings = services.get_settings()
    # The backfill decision reads pre-success state: the first listing queues
    # only the newest cap posts (the listing arrives newest first) unless the
    # follow asked for the whole window.
    candidates = posts if (source.backfill_done or source.full_backfill) \
        else posts[:settings.follow_backfill_cap]
    candidate_ids = {post["id"] for post in candidates}

    queued = 0
    try:
        for post in posts:
            with transaction.atomic():
                try:
                    with transaction.atomic():
                        SeenPost.objects.create(
                            source=source, post_id=post["id"],
                            post_url=post["url"][:MAX_URL_LENGTH])
                except IntegrityError:
                    # A concurrent listing won: the post is already handled.
                    # The inner atomic is a savepoint, so the outer block
                    # stays usable.
                    continue
                if post["id"] not in candidate_ids:
                    continue
                post_url = post["url"]
                normalized = lib.norm(post_url)
                if not normalized:
                    continue  # malformed entry: seen, never queued
                try:
                    services._require_url(normalized)
                except ValidationError:
                    continue
                crid = uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{source.id}:{post['id']}")
                services.accept_capture(source.user, crid, post_url, None,
                                        follow=source)
            # The marker and the capture committed together: the post is
            # queued.
            queued += 1
    except Exception as exc:
        # A capture failure rolls the marker back with its capture (nothing
        # consumed) and takes the listing-failure path. Without the backoff
        # write the source would stay due and the coordinator would re-list
        # in a tight loop; the next scheduled listing retries the post.
        _fail_cycle(source, say, exc, "capture failed")
        return "error"

    now = timezone.now()
    with transaction.atomic():
        source.last_check = now
        source.last_error = ""
        source.failures = 0
        source.backfill_done = True
        source.next_check = now + timedelta(seconds=settings.follow_poll_seconds)
        source.save(update_fields=["last_check", "last_error", "failures",
                                   "backfill_done", "next_check"])
    if say:
        say(f"follow {source.id}: ok ({len(posts)} posts, {queued} queued)")
    return "ok"
