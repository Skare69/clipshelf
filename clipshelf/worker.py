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
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path, PureWindowsPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from clipshelf import acquisition, interpretation, lib, models, services
from clipshelf.management.locks import coordinator_lock, data_lock

log = logging.getLogger(__name__)

CONFIG_ERROR = "llm-not-configured"  # blocked marker; claim resumes when config exists
MAX_ATTEMPTS = 5
POLL_SECONDS = 5
MAX_IMPORT_ITEMS = 5000  # own payload bound; acquisition.import_export batches at 500
IMPORT_BATCH = 500


def _data(*parts):
    return Path(settings.DATA_DIR).joinpath(*parts)


def _concurrency():
    try:
        n = int(services.get_settings().llm_concurrency)
    except Exception:  # singleton missing during early bootstrap
        n = 2
    return max(1, min(n, 8))


def _backoff(attempts):
    return timedelta(seconds=min(30 * 2 ** max(0, attempts - 1), 3600))


def _material(job):
    """Job source with asset paths resolved for reading.

    store_source keeps DATA_DIR-relative paths; the interpreter opens files
    directly, so hand it absolute paths under DATA_DIR (nothing else changes).
    """
    source = dict(job.source or {})
    assets = []
    for asset in source.get("assets") or []:
        path = asset.get("path")
        assets.append({**asset, "path": str(_data(path))} if isinstance(path, str) else asset)
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
        recovered = _recover_abandoned()
        if recovered:
            say(f"recovered {recovered} abandoned running job(s)")
        pool = ThreadPoolExecutor(max_workers=_concurrency(), thread_name_prefix="clipshelf-worker")
        try:
            while True:
                imports = _claim_imports()
                jobs = _claim_batch(_concurrency())
                if not imports and not jobs:
                    if once:
                        break
                    time.sleep(POLL_SECONDS)
                    continue
                futures = [pool.submit(_run_import, r, say) for r in imports]
                futures += [pool.submit(_process, job) for job in jobs]
                for f in futures:
                    f.result()
        except KeyboardInterrupt:
            say("interrupted: finishing in-flight jobs; remaining work stays queued")
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def _recover_abandoned():
    """Recover in-flight work while holding the exclusive coordinator lock."""
    with transaction.atomic():
        recovered = models.Job.objects.filter(state="running").update(
            state="retry", retry_at=timezone.now()
        )
        for record in models.ImportRecord.objects.filter(manifest__status="running"):
            _update_manifest(record, {"status": "pending"})
            recovered += 1
        return recovered


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
            job.state = "running"
            job.retry_at = None
            job.save()
            return job
    except models.Job.DoesNotExist:
        return None


def _set(job, **fields):
    for key, value in fields.items():
        setattr(job, key, value)
    job.save()


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
        _set(job, state="queued", retry_at=timezone.now() + timedelta(seconds=POLL_SECONDS),
             error="paused: account or collection access changed")
        return
    if job.acquisition in ("blocked", "error"):
        # re-claimed imported job: upstream refused, interpretation needs a
        # usable source; keep blocked for manual retry/import, never hang in running
        _set(job, state="blocked")
        return
    if job.acquisition == "pending":
        if not _acquire_phase(job):
            return
    if job.interpretation == "pending" and job.acquisition in ("complete", "partial"):
        _interpret_phase(job)
        return
    if job.acquisition != "pending" and job.interpretation != "pending":
        _set(job, state="done", error="")


def _acquire_phase(job):
    """Fetch into staging, publish complete files to their final paths, then
    commit source + assets in one short transaction."""
    staging = _data("staging", f"job-{job.id}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        source = acquisition.acquire(job.url, str(staging))
    except acquisition.AcquisitionError as exc:
        _fail(job, exc)
        return False
    _publish(job, source)
    status = source.get("acquisition") or "error"
    with transaction.atomic():
        services.store_source(job, source)
        job.final_url = source.get("url") or job.final_url or job.url
        job.acquisition = status
        job.warnings = _warn(job, *source.get("warnings", []))
        job.save()
    if status in ("blocked", "error"):
        _set(job, state="blocked")  # honest: saved, needs manual retry/import
        return False
    return True


def _publish(job, source):
    """Publish one acquisition into a fresh directory, never over retained bytes."""
    root = _data("assets", str(job.id), uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=True)
    assets = source.get("assets") or []
    for i, asset in enumerate(assets):
        src = Path(asset["path"]).resolve()
        if not src.is_relative_to(_data("staging", f"job-{job.id}").resolve()):
            raise ValidationError("acquired asset is outside its staging directory")
        dest = root / f"{i}-{src.name}"
        os.replace(src, dest)  # atomic: file appears complete at final path
        assets[i] = {**asset, "path": str(dest)}
    if assets:
        source["assets"] = assets
    return len(assets)


def _interpret_phase(job):
    if lib.is_terminal(job.url):
        _set(job, interpretation="blocked", state="done", error="",
             warnings=_warn(job, "terminal host: retained as metadata only, never interpreted"))
        return
    cfg = services.get_settings()
    if not (cfg.llm_base_url and cfg.llm_model):
        _set(job, state="blocked", error=CONFIG_ERROR, retry_at=None)
        return
    try:
        findings = interpretation.interpret(
            _material(job), cfg.llm_config(), _categories(job.collection))
    except (interpretation.ConfigurationError, interpretation.InterpretationError) as exc:
        _fail(job, exc)
        return
    # store_findings owns the transaction and final job state, including access loss.
    services.store_findings(job, findings)


def _categories(collection):
    rows = (models.Contribution.objects.filter(entry__collection=collection)
            .exclude(data__cat__isnull=True).exclude(data__cat="")
            .values_list("data__cat", flat=True).distinct()[:200])
    return [c for c in rows if isinstance(c, str)]


def _fail(job, exc):
    with transaction.atomic():
        job.attempts = (job.attempts or 0) + 1
        job.error = (str(exc) or exc.__class__.__name__)[:2000]
        if job.attempts >= MAX_ATTEMPTS:
            job.state = "blocked"
            job.retry_at = None
        else:
            job.state = "retry"
            job.retry_at = timezone.now() + _backoff(job.attempts)
        job.save()
    log.warning("job %s attempt %d failed: %s", job.id, job.attempts, job.error)


# ------------------------------------------------------------ browser import

def _claim_imports(limit=4):
    """Atomically claim pending ImportRecords queued by the upload endpoint."""
    claimed = []
    with transaction.atomic():
        records = (models.ImportRecord.objects
                   .filter(manifest__status="pending")
                   .select_for_update()[:limit])
        for rec in records:
            manifest = dict(rec.manifest)
            manifest["status"] = "running"
            rec.manifest = manifest
            rec.save(update_fields=["manifest"])
            claimed.append(rec)
    return claimed


def _run_import(record, say=None):
    try:
        result = run_staged_import(record)
        _update_manifest(record, {"status": "done", "result": result})
        if say:
            say(f"import {record.id}: done ({result.get('counts', {})})")
    except Exception as exc:
        log.exception("import %s failed", record.id)
        _update_manifest(record, {"status": "error", "error": str(exc)[:2000]})
        if say:
            say(f"import {record.id}: failed ({exc})")


def _update_manifest(record, updates):
    with transaction.atomic():
        record = models.ImportRecord.objects.select_for_update().get(pk=record.pk)
        manifest = dict(record.manifest)
        manifest.update(updates)
        record.manifest = manifest
        record.save(update_fields=["manifest"])


def run_staged_import(record):
    """Process one upload-staged import record created by the HTTP surface."""
    manifest = record.manifest or {}
    staging_rel = manifest.get("staging") or ""
    path = _data(staging_rel)
    root = Path(settings.DATA_DIR).resolve()
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValidationError("import record staging path is missing or outside DATA_DIR")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") == "legacy" or isinstance(payload, dict):
        # Legacy library.json upload: media referenced as cache/<sha1>.<ext> is
        # resolved from <DATA_DIR>/import-cache — copy the old installation's
        # cache/ directory there before importing to retain its media;
        # unresolved files are recorded in the manifest's missing list.
        args = legacy_payload(payload)
        cache = _data("import-cache")
        return import_items(
            user=record.user,
            collection_id=manifest.get("collection_id"),
            items=args["items"], prompts=args["prompts"],
            seen=args["seen"], removed=args["removed"],
            client_request_id=manifest.get("client_request_id"),
            record=record,
            cache_dir=str(cache) if cache.is_dir() else None,
            origin="legacy-import",
        )
    if not isinstance(payload, list):
        raise ValidationError("import payload must be a JSON list")
    return import_items(
        user=record.user,
        collection_id=manifest.get("collection_id"),
        items=payload,
        client_request_id=manifest.get("client_request_id"),
        record=record,
        origin="browser-import",
    )


# ------------------------------------------------------------- import engine

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _legacy_media_path(rel, cache_dir):
    """Existing file for a legacy reference, or None.

    library.json stores repo-relative paths ("cache/<sha1>.<ext>") while
    --cache points at the cache directory itself, so both spellings resolve.
    """
    relative = PureWindowsPath(rel)
    if relative.drive or relative.root or ".." in relative.parts:
        raise ValidationError("legacy media must be relative to the cache directory")
    if not cache_dir:
        return None
    base = Path(cache_dir).resolve()
    for candidate in (base / rel, base / Path(rel).name):
        candidate = candidate.resolve()
        if not candidate.is_relative_to(base):
            raise ValidationError("legacy media is outside the cache directory")
        if candidate.is_file():
            return candidate
    return None


def _resolve_media(value, cache_dir, staging, files_record):
    """Normalize one media reference to a Sources-accepted spec.

    Accepts {"path"|"b64"|"url"} dicts (browser shape), http(s) strings, and
    legacy cache-relative paths ("cache/<sha1>.<ext>") resolved against
    cache_dir and copied into staging under DATA_DIR. Returns None when a
    referenced file is missing (recorded honestly in files_record).
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, dict):
        if "path" not in value:
            return value
        value = value["path"]
        if not isinstance(value, str):
            raise ValidationError("media path must be a relative filename")
    if isinstance(value, list):  # legacy: images lists handled per entry
        return None
    text = str(value).strip()
    if text.startswith(("http://", "https://")):
        return {"url": text}
    rel = text.replace("\\", "/")
    src = _legacy_media_path(rel, cache_dir)
    if src is None:
        files_record["missing"].append(rel)
        return None
    digest = _sha256_file(src)
    dest = staging / f"{digest}{src.suffix}"
    if not dest.exists():
        shutil.copy2(src, dest)
    files_record["files"][rel] = {"sha256": digest, "size": src.stat().st_size}
    return {"path": str(dest)}


def _stage_items(items, cache_dir, staging):
    """Resolve legacy cache references into staged absolute-path specs."""
    files_record = {"files": {}, "missing": []}
    staged = []
    for item in items:
        item = dict(item)
        for field in ("images",):
            values = item.get(field)
            if isinstance(values, list):
                specs = [s for s in (_resolve_media(v, cache_dir, staging, files_record)
                                     for v in values) if s]
                if specs:
                    item[field] = specs
                else:
                    item.pop(field, None)
        for field in ("video", "cache"):
            spec = _resolve_media(item.get(field), cache_dir, staging, files_record)
            if spec:
                item[field] = spec
            else:
                item.pop(field, None)
        staged.append(item)
    return staged, files_record


def _append_page_asset(source, item):
    """Sources validates images/video only; retain the cached page HTML as a
    page-kind asset with a dense position (Sources expects 0..n)."""
    spec = item.get("cache")
    if isinstance(spec, dict) and spec.get("path") and Path(spec["path"]).is_file():
        assets = source.setdefault("assets", [])
        assets.append({"path": spec["path"], "kind": "page",
                       "content_type": "text/html", "position": len(assets)})


def _legacy_findings(item, url):
    """Reconstruct findings-shaped data from legacy merged entry fields.

    The findings contract stores categories/installs as string lists; the
    legacy per-repo mapping collapses to this entry's own values.
    """
    return {"source": url, "summary": item.get("desc") or "", "repos": [],
            "prompts": [], "links": [],
            "categories": [item["cat"]] if item.get("cat") else [],
            "installs": [item["install"]] if item.get("install") else [],
            "warnings": ["migrated from legacy library; findings merged into entry fields"]}


def _prompt_key(text):
    return hashlib.sha1(" ".join(str(text).split()).lower().encode()).hexdigest()[:16]


LEGACY_FIELDS = ("title", "desc", "sources", "tags", "found", "interpreted",
                 "pending", "cat", "install", "archived", "stars")


def legacy_payload(data):
    """Translate a legacy library.json object into import_items arguments."""
    if not isinstance(data, dict):
        raise ValidationError("library.json must be a JSON object")
    items = []
    for url, entry in data.get("links", {}).items():
        item = {"url": url}
        item.update({k: entry[k] for k in LEGACY_FIELDS if entry.get(k) not in (None, "", [])})
        for k in ("images", "video", "cache"):
            if entry.get(k):
                item[k] = entry[k]
        items.append(item)
    prompts = [p for p in data.get("prompts", {}).values() if isinstance(p, dict)]
    return {"items": items, "prompts": prompts,
            "seen": data.get("seen", {}), "removed": data.get("removed", {})}


def import_items(*, user, collection_id, items, prompts=None, client_request_id=None,
                 record=None, cache_dir=None, origin="legacy-import",
                 seen=None, removed=None, dry_run=False):
    """Validate and import legacy/tiktok items into one collection.

    All-or-nothing: validation (including media staging and public fetch of
    missing bytes) completes before any database write; the DB commit is one
    transaction, so a crash or failure leaves no partial contributions and an
    exact repeat is a no-op keyed by (user, input digest). Raises
    ValidationError for rejected payloads; returns a result dict.
    """
    if not isinstance(items, list):
        raise ValidationError("import payload must be a list of items")
    if len(items) > MAX_IMPORT_ITEMS:
        raise ValidationError(f"too many items ({len(items)} > {MAX_IMPORT_ITEMS})")
    collection, notice = services.destination_for(user, collection_id)

    seen = dict(seen or {})
    removed_keys = {lib.norm(u) or u for u in (removed or {})}
    items = _dedupe(items, removed_keys)
    prompts = list(prompts or [])

    digest = _input_digest(items, prompts, cache_dir)
    existing = (None if dry_run else
                models.ImportRecord.objects.filter(user=user, input_digest=digest).first())
    if existing is not None:
        return {"created": False, "digest": digest, "manifest": existing.manifest,
                "notice": notice, "collection_id": str(collection.id)}

    staging = _data("staging", f"import-{uuid.uuid4().hex}")
    if dry_run:
        files_record = {"files": {}, "missing": []}
        for item in items:  # read-only: digest referenced cache files in place
            for value in ([*(item.get("images") or []), item.get("video"), item.get("cache")]):
                if isinstance(value, str) and not value.startswith(("http://", "https://")):
                    src = _legacy_media_path(value.replace("\\", "/"), cache_dir)
                    if src is not None:
                        files_record["files"][value] = {
                            "sha256": _sha256_file(src), "size": src.stat().st_size}
                    else:
                        files_record["missing"].append(value)
        manifest = _manifest(items, prompts, seen, removed_keys, files_record, origin)
        return {"created": False, "dry_run": True, "digest": digest, "manifest": manifest,
                "notice": notice, "collection_id": str(collection.id)}

    staging.mkdir(parents=True, exist_ok=True)
    try:
        staged, files_record = _stage_items(items, cache_dir, staging)
        sources = []
        for i in range(0, len(staged), IMPORT_BATCH):
            sources.extend(acquisition.import_export(staged[i:i + IMPORT_BATCH], str(staging)))
        if len(sources) != len(staged):
            raise ValidationError("import validation returned mismatched results")
        plans = _plans(staged, sources)
        prompt_plans = _prompt_plans(prompts)
        result = _commit_import(user, collection, plans, prompt_plans, digest, origin,
                                client_request_id, record, notice, staging, files_record,
                                seen_map=seen, removed_keys=removed_keys)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result


def _dedupe(items, removed_keys):
    seen_keys = set()
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("url"):
            raise ValidationError("every import item needs a dict with a url")
        key = lib.norm(str(item["url"])) or str(item["url"])
        if key in removed_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        item = dict(item)
        item["url"] = key
        out.append(item)
    return out


def _input_digest(items, prompts, cache_dir):
    """Content digest of the input plus the bytes of referenced cache files,
    so identical input with mutated cache is (honestly) a different import."""
    h = hashlib.sha256()
    h.update(b"clipshelf-import-v1\n")
    h.update(_canonical(items).encode())
    h.update(_canonical(prompts).encode())
    h.update((str(cache_dir) if cache_dir else "-").encode())
    return h.hexdigest()


def _manifest(items, prompts, seen, removed_keys, files_record, origin):
    links = {}
    for item in items:
        url = str(item["url"])
        fields = {k: v for k, v in item.items()
                  if k not in ("images", "video", "cache", "b64")}
        links[url] = {
            "fields_sha256": _sha256_bytes(_canonical(fields).encode()),
            "title": item.get("title", ""), "found": item.get("found", ""),
            "interpreted": item.get("interpreted", ""),
            "pending": bool(item.get("pending")),
            "sources": item.get("sources", []), "tags": item.get("tags", []),
            "cat": item.get("cat", ""), "install": item.get("install", ""),
        }
    prompt_manifest = {}
    for p in prompts:
        key = _prompt_key(p.get("text", ""))
        prompt_manifest[key] = {
            "fields_sha256": _sha256_bytes(_canonical(p).encode()),
            "title": p.get("title", ""), "cat": p.get("cat", ""),
            "found": p.get("found", ""), "interpreted": p.get("interpreted", ""),
        }
    return {
        "generated_at": timezone.now().isoformat(),
        "origin": origin,
        "counts": {
            "links": len(links), "prompts": len(prompt_manifest),
            "seen": len(seen), "removed": len(removed_keys),
            "media_files": len(files_record["files"]),
            "missing_media": len(files_record["missing"]),
        },
        "media_files": files_record["files"],
        "missing_media": files_record["missing"],
        "seen": seen,  # verbatim: includes redirect aliases recorded as seen
        "removed": sorted(removed_keys),
        "legacy_settings": {"interpret_cmd_recorded_only": True},
        "links": links,
        "prompts": prompt_manifest,
    }


def _plans(items, sources):
    """Pair each staged item with its validated source and the target job state."""
    plans = []
    for item, source in zip(items, sources):
        _append_page_asset(source, item)
        url = str(item["url"])
        status = source.get("acquisition") or "error"
        interpreted = bool(item.get("interpreted"))
        if interpreted:
            state, interp = "done", "complete"
            findings = _legacy_findings(item, url)
        elif status in ("blocked", "error") and not source.get("assets"):
            state, interp = "blocked", "pending"
            findings = None
        else:
            state, interp = "queued", "pending"  # worker interprets without refetch
            findings = None
        warnings = list(source.get("warnings") or [])
        plans.append({"item": item, "source": source, "url": url, "status": status,
                      "state": state, "interpretation": interp, "findings": findings,
                      "warnings": warnings})
    return plans


def _prompt_plans(prompts):
    plans = []
    for p in prompts:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        plans.append({"key": _prompt_key(text), "data": {
            "title": p.get("title", ""), "text": text,
            "sources": list(p.get("sources") or []), "cat": p.get("cat", ""),
            "found": p.get("found", ""), "interpreted": p.get("interpreted", ""),
        }})
    return plans


def _commit_import(user, collection, plans, prompt_plans, digest, origin,
                   client_request_id, record, notice, staging, files_record,
                   seen_map=None, removed_keys=()):
    """Publish all files, then create every row in one transaction."""
    # Publish job files first: complete bytes at final paths before any reference.
    for plan in plans:
        plan["published"] = _publish_paths(plan["source"], staging)

    crid = client_request_id or str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"clipshelf-import:{user.id}:{digest}"))
    manifest = _manifest([p["item"] for p in plans],
                         [p["data"] for p in prompt_plans],
                         seen_map or {}, removed_keys, files_record, origin)
    counts = {"entries": 0, "jobs": 0, "assets": 0, "prompts": 0}
    with transaction.atomic():
        capture, created = models.Capture.objects.get_or_create(
            user=user, client_request_id=crid,
            defaults={
                "raw_text": f"import {len(plans)} item(s) digest:{digest[:16]}",
                "requested_collection_id": str(collection.id),
                "collection": collection, "request_hash": digest,
                "received_at": timezone.now(), "notice": notice,
            })
        if capture.request_hash != digest:
            raise ValidationError(
                "client_request_id was already used for a different import payload")
        for plan in plans:
            entry, _ = models.Entry.objects.get_or_create(
                collection=collection, kind="link", key=plan["url"])
            counts["entries"] += 1
            job = None
            if plan["published"] or plan["state"] != "done":
                job = models.Job.objects.create(
                    capture=capture, collection=collection, url=plan["url"],
                    final_url=plan["source"].get("url") or plan["url"],
                    state=plan["state"], acquisition=plan["status"],
                    interpretation=plan["interpretation"],
                    source=plan["source"], warnings=plan["warnings"])
                if plan["published"]:
                    services.store_source(job, plan["source"])
                    counts["assets"] += len(plan["published"])
                if plan["findings"]:
                    services.store_findings(job, plan["findings"])
                counts["jobs"] += 1
            data = _legacy_contribution_data(plan["item"])
            models.Contribution.objects.get_or_create(
                entry=entry, user=user, origin=origin,
                defaults={"capture": capture, "job": job, "data": data})
        for plan in prompt_plans:
            entry, _ = models.Entry.objects.get_or_create(
                collection=collection, kind="prompt", key=plan["key"])
            models.Contribution.objects.get_or_create(
                entry=entry, user=user, origin=origin,
                defaults={"capture": capture, "data": plan["data"]})
            counts["prompts"] += 1
            counts["entries"] += 1
        result = {"created": True, "digest": digest, "counts": counts, "manifest": manifest,
                  "notice": notice, "collection_id": str(collection.id)}
        if record is None:
            models.ImportRecord.objects.create(
                user=user, input_digest=digest, manifest=manifest)
        else:
            # Commit the checkpoint with its rows: a restart must not import twice.
            _update_manifest(record, {"status": "done", "result": result})
    return result


def _publish_paths(source, staging):
    """Publish only staged import bytes, isolated from every previous import."""
    if not source.get("assets"):
        return []
    root = _data("assets", uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=True)
    published = []
    for i, asset in enumerate(source["assets"]):
        src = Path(asset["path"]).resolve()
        if not src.is_relative_to(staging.resolve()):
            raise ValidationError("imported asset is outside its staging directory")
        dest = root / f"{i}-{src.name}"
        # Shared cache files can feed several entries; staging is removed by the caller.
        shutil.copy2(src, dest)
        published.append({**asset, "path": str(dest)})
    source["assets"] = published
    return published


def _legacy_contribution_data(item):
    """Keep every useful legacy field on the contribution; media now lives in
    published assets, so drop the transient path specs."""
    skip = {"images", "video", "cache", "b64", "url"}
    data = {k: v for k, v in item.items() if k not in skip}
    data.setdefault("title", "")
    data.setdefault("desc", "")
    data.setdefault("sources", [])
    data.setdefault("tags", [])
    return data


# ------------------------------------------------------- extracted-merge flow

def merge_findings(user, findings, origin="extracted"):
    """Merge interpret-skill findings into the user's Personal collection with
    legacy add_extracted semantics (longer text wins, tags/sources union)."""
    personal = services.personal_collection(user)
    stats = {"links": 0, "prompts": 0, "updated": 0}
    now = timezone.now()
    with transaction.atomic():
        for finding in findings:
            source = lib.norm(str(finding.get("source", "")))
            cats = finding.get("categories") or {}
            installs = finding.get("installs") or {}
            if source:
                entry, _ = models.Entry.objects.get_or_create(
                    collection=personal, kind="link", key=source)
                if _merge_own(entry, user, origin, summary=finding.get("summary"),
                              sources=[source], now=now):
                    stats["updated"] += 1
            for repo in finding.get("repos") or []:
                url = lib.repo_url(repo)
                if not url:
                    continue
                entry, _ = models.Entry.objects.get_or_create(
                    collection=personal, kind="link", key=url)
                if _merge_own(entry, user, origin, sources=[source] if source else [],
                              tags=["github"], cat=cats.get(repo), install=installs.get(repo),
                              now=now):
                    stats["links"] += 1
            for link in finding.get("links") or []:
                url = lib.norm(str(link.get("url", "")))
                if not url:
                    continue
                entry, _ = models.Entry.objects.get_or_create(
                    collection=personal, kind="link", key=url)
                title = " ".join(str(link.get("title", "")).split())
                if _merge_own(entry, user, origin, title=title, sources=[source] if source else [],
                              tags=[t for t in [lib.tag_for(url, title) or "page"] if t],
                              now=now):
                    stats["links"] += 1
            for prompt in finding.get("prompts") or []:
                text = (prompt.get("text") or "").strip()
                if not text:
                    continue
                entry, _ = models.Entry.objects.get_or_create(
                    collection=personal, kind="prompt", key=_prompt_key(text))
                if _merge_own(entry, user, origin, prompt_text=text,
                              title=prompt.get("title", ""), cat=prompt.get("cat", ""),
                              sources=[source] if source else [], now=now):
                    stats["prompts"] += 1
    return stats


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
    return True
