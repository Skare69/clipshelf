"""One worker coordinator for capture jobs and import records.

Exclusive coordinator (OS lock), bounded thread pool, atomic job claims,
abandoned-running recovery on restart. Network and LLM calls happen strictly
outside database transactions. Asset bytes are published complete at their
final path (os.replace) before any DB reference commits, so a crash can never
leave "done" pointing at partial files. Disabled accounts pause instead of
processing; lost collection rights pause instead of processing.

Finite failure policy: transient errors retry with capped exponential backoff
up to MAX_ATTEMPTS, then block. Missing LLM settings use the CONFIG_ERROR
marker (attempts untouched) and resume when configured. Invalid settings and
endpoint failures use the finite failure policy. Malformed output is retried once
inside clipshelf.interpretation, then preserved as an actionable failure with
the last good findings intact.
"""
import logging
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from clipshelf import (
    acquisition,
    asset_files,
    imports,
    interpretation,
    lib,
    models,
    services,
)
from clipshelf.management.locks import coordinator_lock, data_lock

log = logging.getLogger(__name__)

CONFIG_ERROR = "llm-not-configured"  # blocked marker; claim resumes when config exists
# MAX_ATTEMPTS moved to models.Job; the finite-failure policy lives in Job.fail.
POLL_SECONDS = 5


def _concurrency():
    try:
        n = int(services.get_settings().llm_concurrency)
    except Exception:  # singleton missing during early bootstrap
        n = 2
    return max(1, min(n, 8))


def _material(job):
    """Job source with asset paths resolved for reading.

    store_source keeps DATA_DIR-relative paths; the interpreter opens files
    directly, so hand it absolute paths under DATA_DIR (nothing else changes).
    """
    source = dict(job.source or {})
    assets = []
    for asset in source.get("assets") or []:
        path = asset.get("path")
        if not isinstance(path, str):
            assets.append(asset)
            continue
        try:
            assets.append({**asset, "path": str(asset_files.resolve_path(path))})
        except ValidationError:
            continue  # unresolvable stored path: interpret without that file
    if assets:
        source["assets"] = assets
    return source


# ---------------------------------------------------------------- coordinator

def main(once=False, stdout=None):
    """Run the worker coordinator. once=True drains currently claimable work."""

    def say(message):
        if stdout is not None:
            stdout.write(str(message))
        else:
            print(message)

    with coordinator_lock(), data_lock():
        job_ids, import_ids = _recover_abandoned()
        if job_ids or import_ids:
            say(f"recovered abandoned jobs {job_ids} and imports {import_ids}")
        pool = ThreadPoolExecutor(max_workers=_concurrency(), thread_name_prefix="clipshelf-worker")
        # Refill keeps a slot busy while a long import runs; --once still
        # drains every claimable job before breaking. Imports never take
        # the whole pool: at least one slot stays free for captures.
        pending = set()
        import_futures = set()
        try:
            while True:
                free = _concurrency() - len(pending)
                if free > 0:
                    slots = min(free, max(1, _concurrency() - 1) - len(import_futures))
                    claimed = _claim_imports(slots) if slots > 0 else []
                    for record in claimed:
                        future = pool.submit(_run_import, record, say)
                        import_futures.add(future)
                        pending.add(future)
                    pending.update(pool.submit(_process, job)
                                   for job in _claim_batch(free - len(claimed)))
                if not pending:
                    if once:
                        break
                    time.sleep(POLL_SECONDS)
                    continue
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                pending.difference_update(done)
                import_futures -= done
                for f in done:
                    f.result()
        except KeyboardInterrupt:
            say("interrupted: finishing in-flight jobs; remaining work stays queued")
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def _recover_abandoned():
    """Recover in-flight work while holding the exclusive coordinator lock."""
    with transaction.atomic():
        jobs = list(models.Job.objects.filter(state="running"))
        for job in jobs:
            # The abandoned run counts as a failed attempt, so a job whose
            # processing kills the worker reaches the same blocked terminal
            # state as repeated failures instead of looping across restarts.
            job.fail("worker stopped while processing")
        import_ids = []
        for record in models.ImportRecord.objects.filter(
                manifest__status=models.ImportRecord.Status.RUNNING):
            attempts = int(record.manifest.get("attempts") or 0) + 1
            if attempts >= models.Job.MAX_ATTEMPTS:
                updates = {"status": models.ImportRecord.Status.ERROR,
                           "attempts": attempts,
                           "completed_at": timezone.now().isoformat(),
                           "error": "worker stopped while processing"}
            else:
                updates = {"status": models.ImportRecord.Status.PENDING,
                           "attempts": attempts}
            record.update_manifest(updates)
            import_ids.append(record.id)
        return [job.id for job in jobs], import_ids


def _llm_configured():
    cfg = services.get_settings()
    return bool(cfg.llm_base_url and cfg.llm_model)


def _claimable_jobs():
    due = Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now())
    ready = Q(state__in=("queued", "retry")) & due
    if _llm_configured():
        ready |= Q(state="blocked", error=CONFIG_ERROR)
    return models.Job.objects.filter(ready, capture__user__is_active=True)


def _claim_batch(limit):
    candidates = (_claimable_jobs().order_by("retry_at", "id")
                  .values_list("id", flat=True)[:limit * 4])
    claimed = []
    for jid in candidates:
        job = _claim(jid)
        if job is not None:
            claimed.append(job)
            if len(claimed) >= limit:
                break
    return claimed


def _claim(jid):
    """Atomically flip one claimable job to running, or leave it alone."""
    try:
        with transaction.atomic():
            job = _claimable_jobs().select_for_update().get(pk=jid)
            job.mark_running()
            return job
    except models.Job.DoesNotExist:
        return None


def _warn(job, *messages):
    return list(dict.fromkeys((job.warnings or []) + [m for m in messages if m]))[:100]


# -------------------------------------------------------------- job pipeline

def _process(job):
    try:
        _process_job(job)
    except Exception as exc:  # one bad job must never kill the coordinator
        log.exception("job %s crashed", job.id)
        _fail(job, exc)


def _process_job(job):
    if not services.can_write(job.capture.user, job.collection):
        job.defer(timedelta(seconds=POLL_SECONDS),
                  error="paused: account or collection access changed")
        return
    if job.acquisition == "blocked" or (
            job.acquisition == "error" and job.attempts == 0):
        # Imported or deterministic upstream refusal: no usable source to retry.
        job.mark_blocked()
        return
    if job.acquisition in ("pending", "error"):
        if not _acquire_phase(job):
            return
    if job.interpretation == "pending" and job.acquisition in ("complete", "partial"):
        _interpret_phase(job)
        return
    if job.acquisition != "pending" and job.interpretation != "pending":
        job.mark_done()


def _acquire_phase(job):
    """Fetch into staging, publish complete files to their final paths, then
    commit source + assets in one short transaction."""
    staging = Path(settings.DATA_DIR).joinpath("staging", f"job-{job.id}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        source = acquisition.acquire(job.url, str(staging))
    except acquisition.AcquisitionError as exc:
        _fail(job, exc)
        return False
    # An imported job keeps the export's title and note where the page has none.
    source["title"] = source["title"] or job.source.get("title", "")
    source["desc"] = source["desc"] or job.source.get("desc", "")
    asset_files.publish(source, staging, move=True)
    status = source.get("acquisition") or "error"
    # store_source owns every source fact: final_url, acquisition and warnings.
    services.store_source(job, source)
    if status == "blocked":
        job.mark_blocked()  # deterministic refusal: no automatic retry
        return False
    if status == "error":
        error = "; ".join(job.source.get("warnings") or ["acquisition failed"])
        _fail(job, acquisition.AcquisitionError(error))
        return False
    return True


def _interpret_phase(job):
    if lib.is_terminal(job.url):
        job.mark_done(interpretation=models.Job.InterpretationStatus.BLOCKED,
                      warnings=_warn(job, "terminal host: retained as metadata only, never interpreted"))
        return
    cfg = services.get_settings()
    if not (cfg.llm_base_url and cfg.llm_model):
        job.mark_blocked(error=CONFIG_ERROR)
        return
    try:
        findings = interpretation.interpret(
            _material(job), cfg.llm_config(), _categories(job.collection),
            screening_key=services.screening_key())
    except interpretation.GuardrailBlocked as exc:
        # deterministic block: no attempts, no retry
        job.mark_blocked(error=str(exc)[:2000],
                         interpretation=models.Job.InterpretationStatus.BLOCKED)
        return
    except (interpretation.ConfigurationError, interpretation.InterpretationError) as exc:
        _fail(job, exc)
        return
    # store_findings owns the transaction and final job state, including access loss.
    try:
        services.store_findings(job, findings)
    except interpretation.GuardrailBlocked as exc:
        # screening flagged dangerous findings: deterministic block, no retry
        job.mark_blocked(error=str(exc)[:2000],
                         interpretation=models.Job.InterpretationStatus.BLOCKED)
        return


def _categories(collection):
    """The collection's category vocabulary, most used first, so the model
    reuses a name before it invents one: legacy `cat` plus findings categories."""
    # ponytail: full scan per job; cache the vocabulary if big collections make it slow
    counts = Counter()
    for cat, cats in (models.Contribution.objects.filter(entry__collection=collection)
                      .values_list("data__cat", "data__categories").iterator()):
        counts.update(c for c in [cat, *(cats if isinstance(cats, list) else [])]
                      if interpretation.is_category_label(c))
    return [c for c, _ in counts.most_common()]


def _fail(job, exc):
    with transaction.atomic():
        job.fail(exc)
    log.warning("job %s attempt %d failed: %s", job.id, job.attempts, job.error)


# ---------------------------------------------- staging retention (job acquisition)
# A job owns DATA_DIR/staging/job-<id>: _acquire_phase clears it at the start
# of every attempt and publishes finished bytes out of it. After a terminal
# state nothing reads it again — done jobs moved their assets to final paths;
# blocked jobs keep only rejected or unpublished bytes, which reach the
# 256 MiB direct-link ceiling per attempt. Those bytes are retention material,
# not garbage. Ownership is the Job row, never the directory contents. The one
# retention rule for every staging kind is stated once on
# staging_cleanup_candidates. Nothing here deletes anything.
STAGING_RETENTION_DAYS = 30


def _job_staging_cleanup_candidates(now=None):
    """staging/job-<id> directories owned by terminal (done/blocked) Job
    rows, sorted. Paths must be directories directly under DATA_DIR/staging.
    """
    now = now or timezone.now()
    root = (Path(settings.DATA_DIR) / "staging").resolve()
    cutoff = now - timedelta(days=STAGING_RETENTION_DAYS)
    out = []
    terminal = [models.Job.State.DONE, models.Job.State.BLOCKED]
    for (job_id,) in (models.Job.objects
                      .filter(state__in=terminal, updated_at__lte=cutoff)
                      .values_list("id").iterator()):
        path = (root / f"job-{job_id}").resolve()
        if path.parent == root and path.is_dir():
            out.append(path)
    return sorted(out)


# ------------------------------------------------------------ browser import

def _claim_imports(limit=None):
    """Atomically claim pending ImportRecords queued by the upload endpoint.

    The caller bounds the claim so at least one pool slot stays free for
    capture jobs while imports run (a 1-slot pool still runs one import:
    never claiming would starve uploads instead). Inactive users' imports
    stay unclaimed.
    """
    if limit is None:
        limit = max(1, _concurrency() - 1)
    claimed = []
    with transaction.atomic():
        records = (models.ImportRecord.objects
                   .filter(manifest__status=models.ImportRecord.Status.PENDING,
                           user__is_active=True)
                   .select_for_update()[:limit])
        for rec in records:
            manifest = dict(rec.manifest)
            manifest["status"] = models.ImportRecord.Status.RUNNING
            rec.manifest = manifest
            rec.save(update_fields=["manifest"])
            claimed.append(rec)
    return claimed


def _run_import(record, say=None):
    # Lifecycle: the commit checkpoint in imports._commit_import writes the
    # one "done" update, atomic with the rows it describes. This function
    # only labels a failed import; a failed bookkeeping write is logged and
    # never relabels an already committed import.
    try:
        result = imports.run_staged_import(record)
    except Exception as exc:
        log.exception("import %s failed", record.id)
        try:
            record.update_manifest({"status": models.ImportRecord.Status.ERROR,
                                    "completed_at": timezone.now().isoformat(),
                                    "error": str(exc)[:2000]})
        except Exception:
            log.exception("import %s: status write failed; status unchanged",
                          record.id)
        if say:
            say(f"import {record.id}: failed ({exc})")
        return
    if say:
        say(f"import {record.id}: done ({result.get('counts', {})})")


# ------------------------------------------------- staging retention (review S12)
# Ownership: api_import spools each upload to DATA_DIR/staging/import-<hex>.json
# and the ImportRecord created for it owns that exact path via
# manifest["staging"]; the request deletes its own spooled file when no record
# commits (invalid payload, duplicate digest, claim race). Everything else
# under staging/ (acquisition job-* dirs, import media dirs, stray files) is
# unrelated to import retention.
# Retention: finished records (done/error) keep the upload for replay and
# audit (manifest["completed_at"], else upload time); pending/running records
# still read theirs. The one retention rule for every staging kind is stated
# once on staging_cleanup_candidates. Nothing here deletes anything.


def _staging_finished_at(manifest, created_at):
    """Completion time of a finished import; rows predating the policy fall
    back to their upload time."""
    finished = manifest.get("completed_at")
    parsed = parse_datetime(finished) if isinstance(finished, str) else None
    if parsed is None:
        return created_at
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _staging_sweep_candidates(manifest_paths, accepts, now=None):
    """Shared ImportRecord staging-retention rule.

    manifest_paths(manifest) yields the manifest values naming staging
    paths; accepts(path) is the per-path qualification (containment under
    DATA_DIR/staging plus file/directory kind). A path qualifies only when
    every record naming it is finished (done/error) and at least one owner
    is past the retention window; any live (pending/running) owner keeps
    the path ineligible at any age. Returns accepted paths sorted.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=STAGING_RETENTION_DAYS)
    claims = {}
    finished = {models.ImportRecord.Status.DONE, models.ImportRecord.Status.ERROR}
    for record in models.ImportRecord.objects.only("manifest", "created_at").iterator():
        manifest = record.manifest or {}
        for rel in manifest_paths(manifest):
            if not isinstance(rel, str) or not rel:
                continue
            path = (Path(settings.DATA_DIR) / rel).resolve()
            if not accepts(path):
                continue
            claims.setdefault(path, []).append(
                (manifest.get("status"),
                 _staging_finished_at(manifest, record.created_at)))
    return sorted(
        path for path, owners in claims.items()
        if all(status in finished for status, _ in owners)
        and any(finished_at <= cutoff for _, finished_at in owners))


def _upload_staging_cleanup_candidates(now=None):
    """Staged upload JSON files (manifest["staging"]) a sweep may delete,
    sorted by path. Walks ImportRecord manifests, never the staging
    directory, so a file no record names is unreachable; paths must be files
    under DATA_DIR/staging.
    """
    root = (Path(settings.DATA_DIR) / "staging").resolve()
    return _staging_sweep_candidates(
        lambda manifest: [manifest.get("staging")],
        lambda path: path.is_relative_to(root) and path != root and path.is_file(),
        now=now)


# ---------------------------------------------- staging retention (import media)
# imports.import_items creates a media directory staging/import-<hex>/ next to the
# spooled upload JSON and removes it in a finally block. It records the path
# as manifest["media_staging"] so a crash between creation and cleanup leaves
# an owned, sweepable directory instead of an orphan. A recovered import keeps
# its previous media directory named under manifest["retained_media_staging"]
# so it too stays owned and sweepable. Media directories follow
# the same ownership as the spooled uploads. Nothing here deletes anything.
def _import_media_cleanup_candidates(now=None):
    """Staged import media directories (manifest["media_staging"] plus
    manifest["retained_media_staging"]) a sweep may delete, sorted. Walks
    ImportRecord manifests, never the staging directory, so an unnamed path
    is unreachable; paths must be directories directly under DATA_DIR/staging.
    """
    root = (Path(settings.DATA_DIR) / "staging").resolve()
    return _staging_sweep_candidates(
        lambda manifest: [manifest.get("media_staging"),
                          *(manifest.get("retained_media_staging") or [])],
        lambda path: path.parent == root and path != root and path.is_dir(),
        now=now)


# ------------------------------------------------- staging retention (the rule)
def staging_cleanup_candidates(now=None):
    """Every staging path an operator-approved sweep may delete, sorted: the
    union of job staging dirs, import media dirs and staged upload JSON files.

    The retention rule, stated once for every staging kind: a path is a
    candidate only when a row owns it by name (the Job row or an
    ImportRecord manifest — never the directory contents, so a path no row
    names is unreachable), every owner is finished (done/blocked job,
    done/error import), and at least one owner is past STAGING_RETENTION_DAYS
    after the last state change (job updated_at) or completion
    (manifest["completed_at"], else upload time). A live owner keeps the
    path ineligible at any age. Nothing here deletes anything; the clean
    command lists these paths and deletes only with --execute.
    """
    return sorted(_job_staging_cleanup_candidates(now=now)
                  + _import_media_cleanup_candidates(now=now)
                  + _upload_staging_cleanup_candidates(now=now))

