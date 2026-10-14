"""Worker coordinator regressions: claims, recovery, pauses, finite failures."""
import base64
from contextlib import redirect_stdout
import io
import os
import shutil
import threading
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

# Standalone self-check (`$PY test_worker.py`): models import at module level,
# so Django must be up first. Idempotent under Django's own test discovery.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clipshelf.project.settings")
import django
django.setup()

from PIL import Image

from clipshelf import cli, imports, lib, models, network, publication, services, worker

FINDINGS = {"source": "", "summary": "good findings", "repos": [], "prompts": [],
            "links": [], "categories": [], "installs": [], "warnings": []}


def fake_acquire(url, directory, status="complete"):
    path = Path(directory) / "page.html"
    path.write_text(f"<html>{url}</html>", encoding="utf-8")
    return {"url": url, "original_url": url, "title": "t", "desc": "d", "text": "x",
            "links": [], "assets": [{"path": str(path), "kind": "page",
                                     "content_type": "text/html", "position": 0}],
            "acquisition": status, "warnings": [], "metadata": {}}


class WorkerMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clipshelf-test-"))
        self.user = models.User.objects.create_user(username="w1", email="w1@example.com")
        self.personal = services.personal_collection(self.user)
        cfg = services.get_settings()
        cfg.llm_base_url = "http://llm.test"
        cfg.llm_model = "test-model"
        cfg.save()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_job(self, url="https://example.com/a", collection=None, **fields):
        capture = models.Capture.objects.create(
            user=self.user, client_request_id=str(uuid.uuid4()), raw_text=url,
            requested_collection_id=None, collection=collection or self.personal,
            request_hash="h", received_at=timezone.now(), notice="")
        defaults = dict(state="queued", acquisition="pending", interpretation="pending",
                        attempts=0, collection=collection or self.personal)
        defaults.update(fields)
        return models.Job.objects.create(capture=capture, url=url, **defaults)

    def run_worker(self, interpret=None, acquire=fake_acquire):
        side = interpret or (lambda source, config, cats, **kw: {**FINDINGS, "source": source.get("url")})
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=acquire), \
                mock.patch("clipshelf.interpretation.interpret", side_effect=side):
            worker.main(once=True)

    def refresh(self, job):
        job.refresh_from_db()
        return job


class HarvestedLinkLengthTests(WorkerMixin, TransactionTestCase):
    """One over-long href must not fail the whole capture: the long link is
    dropped at harvest instead of raising in store_source, which used to burn
    every retry and orphan each attempt's published asset directory."""

    def test_over_long_href_is_dropped_capture_completes(self):
        page_url = "https://example.com/page"
        kept = "https://example.com/kept"
        long_url = "https://calendar.example.com/render?" + "x" * 2100
        self.assertGreater(len(long_url), lib.MAX_URL_LENGTH)
        html = (f'<html><head><title>t</title></head><body>'
                f'<a href="{kept}">kept</a>'
                f'<a href="{long_url}">add to calendar</a></body></html>').encode()

        def fake_fetch(url, max_bytes=None, timeout=None, user_agent=None):
            return {"url": url, "body": html, "content_type": "text/html"}

        job = self.make_job(url=page_url)
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.network.fetch_public", side_effect=fake_fetch), \
                mock.patch("clipshelf.interpretation.interpret",
                           side_effect=lambda source, config, cats, **kw:
                               {**FINDINGS, "source": source.get("url")}):
            worker.main(once=True)
        job = self.refresh(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, key=publication.link_key(page_url)).exists())
        self.assertEqual([link["url"] for link in job.source["links"]], [kept])
        self.assertIn(f"page link dropped: longer than {lib.MAX_URL_LENGTH} characters",
                      job.source["warnings"])
        # no orphaned asset directories: every published dir has an Asset row
        on_disk = {p.name for p in (self.tmp / "assets").iterdir()}
        referenced = {asset.path.split("/")[1] for asset in models.Asset.objects.all()}
        self.assertEqual(on_disk, referenced)
        self.assertEqual(models.Asset.objects.count(), 1)


class JobTransitionTests(WorkerMixin, TestCase):
    """The lifecycle methods are the one owner of state rewrites; pin their
    policy boundaries here."""

    def test_fail_bounds_retries_then_blocks(self):
        job = self.make_job()
        for attempt in range(1, models.Job.MAX_ATTEMPTS):
            job.fail(Exception(f"boom {attempt}"))
            self.assertEqual(job.state, "retry")
            self.assertEqual(job.attempts, attempt)
            self.assertIsNotNone(job.retry_at)
        job.fail(Exception("final"))
        self.assertEqual(job.state, "blocked")
        self.assertEqual(job.attempts, models.Job.MAX_ATTEMPTS)
        self.assertIsNone(job.retry_at)
        self.assertEqual(job.error, "final")

    def test_requeue_refuses_active_and_moves_terminal(self):
        job = self.make_job(state="running")
        with self.assertRaises(ValueError):
            job.requeue()
        self.assertEqual(job.state, "running")  # mutation and rule agree
        job.state = "blocked"
        job.error = "kept"  # requeue keeps the error row; guardrail derives from state
        job.save(update_fields=["state", "error"])
        job.requeue()
        self.assertEqual(job.state, "queued")
        self.assertIsNone(job.retry_at)
        self.assertEqual(job.error, "kept")

    def test_mark_blocked_clears_retry_and_keeps_error_when_unspecified(self):
        job = self.make_job(state="retry", retry_at=timezone.now(), error="old")
        job.mark_blocked()
        self.assertEqual(job.state, "blocked")
        self.assertIsNone(job.retry_at)
        self.assertEqual(job.error, "old")
        job.mark_blocked(error="config", interpretation="blocked")
        self.assertEqual(job.error, "config")
        self.assertEqual(job.interpretation, "blocked")

    def test_mark_done_defaults_and_terminal_variant(self):
        job = self.make_job(state="running")
        job.mark_done()
        self.assertEqual(
            (job.state, job.error, job.interpretation),
            ("done", "", "complete"))
        self.assertFalse(job.needs_attention)
        # terminal-host variant: never read, nothing withheld, not attention
        job.mark_done(interpretation="blocked",
                      warnings=["terminal host: retained as metadata only, never interpreted"])
        self.assertEqual(
            (job.state, job.interpretation, job.warnings),
            ("done", "blocked", ["terminal host: retained as metadata only, never interpreted"]))
        self.assertTrue(job.terminal)
        self.assertFalse(job.needs_attention)
        self.assertTrue(job.can_retry)
        # with an error it is a real withheld/failed read again
        job.mark_done(error="upstream refused", interpretation="blocked")
        self.assertFalse(job.terminal)
        self.assertTrue(job.needs_attention)


class WorkerPipelineTests(WorkerMixin, TransactionTestCase):
    def test_pipeline_publishes_assets_before_done(self):
        job = self.make_job()
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertEqual(job.interpretation, "complete")
        self.assertEqual(job.findings["summary"], "good findings")
        asset = models.Asset.objects.get(job=job)
        published = Path(self.tmp) / asset.path
        self.assertTrue(published.is_file(), "no complete asset file at final path")
        self.assertTrue(str(asset.path).startswith("assets/"))

    def test_terminal_host_shelves_job_without_reading(self):
        def no_read(source, config, cats, **kw):
            raise AssertionError("the model must not run for a terminal host")

        job = self.make_job(url="https://arxiv.org/abs/2401.00001")
        self.run_worker(interpret=no_read)
        job = self.refresh(job)
        self.assertEqual(
            (job.state, job.acquisition, job.interpretation, job.error, job.attempts),
            ("done", "complete", "blocked", "", 0))
        self.assertTrue(job.terminal)
        self.assertFalse(job.needs_attention)
        self.assertTrue(job.can_retry)
        self.assertIn("terminal host", job.warnings[0])

    def test_abandoned_running_recovered(self):
        job = self.make_job(state="running")
        self.run_worker()
        # the abandoned run counts as a failed attempt and backs off first
        job = self.refresh(job)
        self.assertEqual((job.state, job.attempts), ("retry", 1))
        self.assertGreater(job.retry_at, timezone.now())
        self.assertEqual(job.error, "worker stopped while processing")
        models.Job.objects.filter(pk=job.pk).update(
            retry_at=timezone.now() - timedelta(seconds=1))
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_recovered_crash_counts_as_attempt(self):
        near_limit = self.make_job(state="running", attempts=4)
        fresh = self.make_job(state="running", attempts=0)
        worker._recover_abandoned()
        near_limit = self.refresh(near_limit)
        self.assertEqual((near_limit.state, near_limit.attempts), ("blocked", 5))
        self.assertIsNone(near_limit.retry_at)
        fresh = self.refresh(fresh)
        self.assertEqual((fresh.state, fresh.attempts), ("retry", 1))
        self.assertGreater(fresh.retry_at, timezone.now())

    def test_recovered_import_counts_attempts_in_manifest(self):
        fresh = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "running"})
        near_limit = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "running", "attempts": 4})
        worker._recover_abandoned()
        fresh.refresh_from_db()
        self.assertEqual(
            (fresh.manifest["status"], fresh.manifest["attempts"]), ("pending", 1))
        near_limit.refresh_from_db()
        self.assertEqual(
            (near_limit.manifest["status"], near_limit.manifest["attempts"]),
            ("error", 5))
        self.assertEqual(near_limit.manifest["error"], "worker stopped while processing")

    def test_disabled_user_pauses_without_processing(self):
        job = self.make_job()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.acquisition, "pending")
        self.assertFalse(models.Asset.objects.filter(job=job).exists())
        self.user.is_active = True
        self.user.save(update_fields=["is_active"])
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_revoked_rights_pause(self):
        other = models.User.objects.create_user(username="w2", email="w2@example.com")
        shared = models.Collection.objects.create(name="s", kind="shared", owner=other)
        models.Membership.objects.create(collection=shared, user=self.user)
        job = self.make_job(collection=shared)
        models.Membership.objects.filter(collection=shared, user=self.user).delete()
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "queued")
        self.assertTrue(job.error.startswith("paused:"))

    def test_missing_config_blocks_without_burning_attempts(self):
        cfg = services.get_settings()
        cfg.llm_base_url = ""
        cfg.save()
        job = self.make_job(acquisition="complete", source={}, warnings=[])
        self.run_worker()
        self.refresh(job)
        self.assertEqual(job.state, "blocked")
        self.assertEqual(job.error, worker.CONFIG_ERROR)
        self.assertEqual(job.attempts, 0)
        cfg.llm_base_url = "http://llm.test"
        cfg.save()
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

    def test_done_requeue_reinterprets(self):
        job = self.make_job()
        self.run_worker()
        calls = []

        def updated(source, config, cats, **kw):
            calls.append(source["url"])
            return {**FINDINGS, "source": source["url"], "summary": "fresh findings"}

        job = self.refresh(job)
        job.requeue()
        self.run_worker(interpret=updated)
        self.refresh(job)
        self.assertEqual(calls, [job.url])
        self.assertEqual((job.state, job.interpretation), ("done", "complete"))
        self.assertEqual(job.findings["summary"], "fresh findings")

    def test_github_repo_is_interpreted_with_the_collection_vocabulary(self):
        other = models.Collection.objects.create(name="o", kind="shared", owner=self.user)
        for collection, data in ((self.personal, {"cat": "ml"}),
                                 (self.personal, {"categories": ["Video generation"]}),
                                 (self.personal, {"categories": ["Video generation", 7]}),
                                 (self.personal, {"cat": "", "categories": "junk"}),
                                 (other, {"categories": ["Secret"]})):
            entry = models.Entry.objects.create(collection=collection, key=str(uuid.uuid4()))
            models.Contribution.objects.create(entry=entry, user=self.user, data=data)
        seen = []

        def interpret(source, config, cats, **kw):
            seen.append(cats)
            return {**FINDINGS, "source": source["url"]}

        job = self.make_job(url="https://github.com/owner/repo")
        self.run_worker(interpret=interpret)
        self.assertEqual(self.refresh(job).interpretation, "complete")
        self.assertEqual(seen, [["Video generation", "ml"]])  # most used first, own collection

    def test_imported_labels_are_bounded_before_prompts(self):
        """An imported legacy `cat` reaches every later capture's prompt as an
        allowed_category, so it must pass the short-visible-label rule."""
        owner = models.User.objects.create_user(username="owner",
                                                email="owner@example.com")
        shared = models.Collection.objects.create(name="Shared", kind="shared",
                                                  owner=owner)
        member = models.User.objects.create_user(username="member",
                                                 email="member@example.com")
        models.Membership.objects.create(user=self.user, collection=shared)
        models.Membership.objects.create(user=member, collection=shared)
        good = "Video generation"
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire):
            result = imports.import_items(
                user=member, collection_id=shared.id,
                items=[{"url": "https://example.com/long", "title": "t",
                        "cat": "x" * 5000, "interpreted": True},
                       {"url": "https://example.com/short", "title": "t",
                        "cat": good, "interpreted": True}])
        self.assertTrue(result["created"])
        seen = []

        def interpret(source, config, cats, **kw):
            seen.append(cats)
            return {**FINDINGS, "source": source["url"]}

        job = self.make_job(collection=shared)
        self.run_worker(interpret=interpret)
        self.assertEqual(self.refresh(job).interpretation, "complete")
        self.assertEqual(seen, [[good]])

    def test_interpretation_failure_preserves_last_good_findings(self):
        job = self.make_job()
        self.run_worker()
        self.assertEqual(self.refresh(job).findings["summary"], "good findings")
        job.state = "queued"  # reprocess: pipeline must keep the last good findings
        job.interpretation = "pending"
        job.save()

        def boom(source, config, cats, **kw):
            from clipshelf import interpretation
            raise interpretation.InterpretationError("malformed output")

        self.run_worker(interpret=boom)
        self.refresh(job)
        self.assertEqual(job.state, "retry")
        self.assertEqual(job.attempts, 1)
        self.assertIn("malformed output", job.error)
        self.assertEqual(job.findings["summary"], "good findings")

    def test_recapture_keeps_categories_when_interpretation_fails(self):
        # A good earlier findings blob must survive a later failed reprocess:
        # the re-acquisition contributes no categories, so its empty list must
        # not overwrite the ones store_findings published.
        job = self.make_job()
        self.run_worker(interpret=lambda source, config, cats, **kw:
                        {**FINDINGS, "source": source["url"],
                         "categories": ["Video generation"]})
        self.assertEqual(self.refresh(job).interpretation, "complete")

        second = self.make_job()  # same URL captured again: new capture, new job
        self.assertEqual(second.url, job.url)

        def boom(source, config, cats, **kw):
            from clipshelf import interpretation
            raise interpretation.InterpretationError("malformed output")

        self.run_worker(interpret=boom)
        self.assertEqual(self.refresh(second).state, "retry")

        entry = models.Entry.objects.get(
            collection=self.personal, key=publication.link_key(job.url))
        self.assertEqual(
            services.serialize_entry(entry, self.user)["cat"], ["Video generation"])
        self.assertEqual(worker._categories(self.personal), ["Video generation"])

    def test_access_lost_during_interpretation_blocks_without_losing_findings(self):
        owner = models.User.objects.create_user(username="owner", email="owner@example.com")
        shared = models.Collection.objects.create(name="Shared", kind="shared", owner=owner)
        models.Membership.objects.create(user=self.user, collection=shared)
        job = self.make_job(collection=shared)
        self.run_worker()
        job = self.refresh(job)
        good = job.findings
        job.state, job.interpretation = "queued", "pending"
        job.save()

        def revoked(source, config, cats, **kw):
            models.Membership.objects.filter(user=self.user, collection=shared).delete()
            return {**FINDINGS, "source": source["url"]}

        self.run_worker(interpret=revoked)
        self.assertEqual(self.refresh(job).state, "blocked")
        self.assertEqual(job.findings, good)

    def test_transport_failures_have_one_bounded_retry_owner(self):
        job = self.make_job(acquisition="complete", source={"url": "https://example.com/a"})
        cfg = services.get_settings()
        cfg.llm_api_key = "private-provider-key"
        cfg.save()

        def refused(request, **kwargs):
            raise HTTPError(request.full_url, 429, "No balance", {},
                            io.BytesIO(b'{"error":"No balance for private-provider-key"}'))

        # Exercise the real interpreter; only the HTTP transport is replaced.
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("urllib.request.urlopen", side_effect=refused) as upstream:
            for attempt in range(1, models.Job.MAX_ATTEMPTS + 1):
                claimed = worker._claim_batch(1)
                self.assertEqual([j.pk for j in claimed], [job.pk])
                worker._process(claimed[0])
                self.refresh(job)
                self.assertEqual(upstream.call_count, attempt)
                self.assertEqual(job.attempts, attempt)
                self.assertIn("429", job.error)
                self.assertNotIn(cfg.llm_api_key, job.error)
                self.assertEqual(worker._claim_batch(1), [])
                if attempt < models.Job.MAX_ATTEMPTS:
                    self.assertEqual(job.state, "retry")
                    self.assertGreater(job.retry_at, timezone.now())
                    job.retry_at = timezone.now() - timezone.timedelta(seconds=1)
                    job.save(update_fields=["retry_at"])
            self.assertEqual(job.state, "blocked")
            self.assertIsNone(job.retry_at)

        # Connection failures use the same policy, without hidden HTTP retries.
        job = self.make_job(acquisition="complete", source={"url": "https://example.com/a"})
        with mock.patch("urllib.request.urlopen", side_effect=URLError("offline")) as upstream:
            worker._process(worker._claim(job.id))
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(self.refresh(job).attempts, 1)
        self.assertEqual(worker._claim_batch(1), [])

    def test_requeued_acquisition_failures_reacquire(self):
        jobs = [
            self.make_job(url=f"https://example.com/{status}", state="blocked",
                          acquisition=status)
            for status in ("blocked", "error")
        ]
        for job in jobs:
            job.requeue()
        acquired = []

        def reacquire(url, directory):
            acquired.append(url)
            return fake_acquire(url, directory)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=reacquire), \
                mock.patch("clipshelf.interpretation.interpret",
                           side_effect=lambda source, config, cats, **kw: {
                               **FINDINGS, "source": source.get("url")}):
            for job in jobs:
                claimed = worker._claim(job.pk)
                self.assertIsNotNone(claimed)
                worker._process(claimed)
        for job in jobs:
            self.refresh(job)
            self.assertEqual((job.state, job.acquisition, job.interpretation),
                             ("done", "complete", "complete"))
        self.assertCountEqual(acquired, [job.url for job in jobs])

    def test_transient_acquisition_error_uses_finite_backoff(self):
        job = self.make_job()

        def temporary_failure(url, directory):
            source = fake_acquire(url, directory, "error")
            source["warnings"] = ["temporary upstream failure"]
            return source

        self.run_worker(acquire=temporary_failure)
        self.refresh(job)
        self.assertEqual((job.state, job.acquisition, job.attempts),
                         ("retry", "error", 1))
        self.assertEqual(job.error, "temporary upstream failure")
        self.assertGreater(job.retry_at, timezone.now())
        self.assertEqual(worker._claim_batch(1), [])

        job.retry_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["retry_at"])
        acquired = []

        def recovered(url, directory):
            acquired.append(url)
            return fake_acquire(url, directory)

        self.run_worker(acquire=recovered)
        self.refresh(job)
        self.assertEqual(acquired, [job.url])
        self.assertEqual((job.state, job.acquisition, job.interpretation),
                         ("done", "complete", "complete"))
        self.assertEqual(job.findings["summary"], "good findings")

    def test_deterministic_acquisition_block_stays_terminal(self):
        job = self.make_job()
        self.run_worker(acquire=lambda url, directory: fake_acquire(
            url, directory, "blocked"))
        self.refresh(job)
        self.assertEqual((job.state, job.acquisition, job.attempts),
                         ("blocked", "blocked", 0))
        self.assertIsNone(job.retry_at)

    def test_invalid_settings_do_not_enter_the_configuration_resume_loop(self):
        cfg = services.get_settings()
        cfg.llm_base_url = "not-a-url"
        cfg.save()
        job = self.make_job(acquisition="complete", source={"url": "https://example.com/a"})
        worker._process(worker._claim(job.id))
        self.assertEqual(self.refresh(job).state, "retry")
        self.assertEqual(job.attempts, 1)
        self.assertEqual(worker._claim_batch(1), [])

    def test_repeating_warning_is_stored_once(self):
        job = self.make_job(warnings=["endpoint rejected request (HTTP 429)"])
        self.assertEqual(
            worker._warn(job, "endpoint rejected request (HTTP 429)", "new one"),
            ["endpoint rejected request (HTTP 429)", "new one"])
        self.assertEqual(len(worker._warn(job, *(["same"] * 300))), 2)

    def test_claim_is_atomic(self):
        job = self.make_job()
        self.assertIsNotNone(worker._claim(job.id))
        self.assertIsNone(worker._claim(job.id))  # already running


class ImportQueueTests(WorkerMixin, TransactionTestCase):
    def test_pending_import_record_processed_idempotently(self):
        staging_rel = f"staging/import-{uuid.uuid4().hex}.json"
        png = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(png, "PNG")
        png_bytes = png.getvalue()
        items = [{"url": "https://example.com/imported", "title": "From browser",
                  "images": [{"b64": base64.b64encode(png_bytes).decode()}]}]
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_text(_json(items), encoding="utf-8")


        record = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending", "staging": staging_rel,
                      "collection_id": str(self.personal.id),
                      "format": "tiktok",
                      "client_request_id": str(uuid.uuid4())})
        # Simulate a coordinator crash after claiming but before importing.
        worker._claim_imports()
        upstream = []

        def blocked_acquire(url, directory):
            upstream.append(url)
            return fake_acquire(url, directory, status="blocked")

        self.run_worker(acquire=blocked_acquire)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(record.manifest["result"]["counts"]["entries"], 1)
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, kind="link",
            key="https://example.com/imported").exists())
        # Imported material is the acquisition: the browser export carried its
        # own media, so the worker interprets the image asset and never
        # re-fetches the post from upstream.
        self.assertEqual(upstream, [])
        job = models.Job.objects.filter(url="https://example.com/imported").get()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertEqual(job.interpretation, "complete")
        asset = models.Asset.objects.get(job=job)
        self.assertEqual((asset.kind, asset.content_type), ("image", "image/png"))
        self.assertEqual((Path(self.tmp) / asset.path).read_bytes(), png_bytes)
        entry = models.Entry.objects.get(collection=self.personal, kind="link",
                                         key="https://example.com/imported")
        contributions = models.Contribution.objects.filter(entry=entry)
        self.assertEqual({c.user_id for c in contributions}, {self.user.id})
        self.assertEqual(
            sum(1 for c in contributions if (c.data or {}).get("findings")), 1)
        # A restart-safe checkpoint: draining again changes nothing.
        before = (models.Job.objects.count(), models.Contribution.objects.count(),
                  models.Entry.objects.count())
        self.run_worker(acquire=blocked_acquire)
        self.assertEqual((models.Job.objects.count(), models.Contribution.objects.count(),
                          models.Entry.objects.count()), before)

    def test_committed_import_survives_a_failed_late_status_write(self):
        """A bookkeeping-write failure after the commit must not relabel a
        committed import as error: the checkpoint write inside the commit
        transaction is the only "done" write (v0.11.0 review finding)."""
        staging_rel = f"staging/import-{uuid.uuid4().hex}.json"
        items = [{"url": "https://example.com/committed", "title": "From browser"}]
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_text(_json(items), encoding="utf-8")
        record = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending", "staging": staging_rel,
                      "collection_id": str(self.personal.id),
                      "format": "tiktok",
                      "client_request_id": str(uuid.uuid4())})
        manifest_saves = []
        real_save = models.ImportRecord.save

        def fault_third_manifest_save(instance, *args, **kwargs):
            update_fields = kwargs.get("update_fields")
            if update_fields and "manifest" in update_fields:
                manifest_saves.append(list(update_fields))
                if len(manifest_saves) == 3:
                    raise OperationalError("database is locked")
            return real_save(instance, *args, **kwargs)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.import_export",
                           side_effect=lambda batch, directory, budget=None:
                               [fake_acquire(item["url"], directory) for item in batch]), \
                mock.patch.object(models.ImportRecord, "save",
                                  fault_third_manifest_save):
            worker._run_import(record)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertTrue(record.manifest["completed_at"])
        self.assertEqual(record.manifest["result"]["counts"]["entries"], 1)
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, kind="link",
            key="https://example.com/committed").exists())
    def _tar_pit_items(self, posts=3):
        """3 items x 106 media URLs (100 slides + 1 video + 5 mirrors), the
        shape the release review flagged as able to hold a slot for days."""
        return [
            {"url": f"https://example.com/post{i}", "title": f"p{i}",
             "images": [{"url": f"https://pit.example/img{j}"} for j in range(100)],
             "video": {"url": "https://pit.example/v0",
                       "urls": [f"https://pit.example/m{j}" for j in range(5)]}}
            for i in range(posts)]

    def _import_jobs(self):
        return {job.url: job for job in models.Job.objects.filter(
            url__startswith="https://example.com/post")}

    def test_time_budget_stops_slow_import_media_honestly(self):
        attempts = []

        def tar_pit(url, **kwargs):
            attempts.append(url)
            time.sleep(0.01)
            raise network.NetworkError("tar-pit host")

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.IMPORT_TIME_BUDGET", 0.15), \
                mock.patch("clipshelf.network.fetch_public", side_effect=tar_pit):
            result = imports.import_items(
                user=self.user, collection_id=self.personal.id,
                items=self._tar_pit_items(), origin="test")
        self.assertTrue(result["created"])
        # The import stopped at the wall-clock budget instead of attempting
        # all 318 fetches against the tar-pit host.
        self.assertLess(len(attempts), 318)
        self.assertGreater(len(attempts), 0)
        jobs = self._import_jobs()
        self.assertEqual(len(jobs), 3)
        for url, job in jobs.items():
            self.assertEqual((job.state, job.acquisition), ("blocked", "error"))
            self.assertIn("import item rejected: import fetch budget exhausted",
                          job.source["warnings"])

    def test_fetch_count_budget_stops_after_bound(self):
        attempts = []

        def tar_pit(url, **kwargs):
            attempts.append(url)
            raise network.NetworkError("tar-pit host")

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.IMPORT_FETCH_BUDGET", 5), \
                mock.patch("clipshelf.network.fetch_public", side_effect=tar_pit):
            imports.import_items(
                user=self.user, collection_id=self.personal.id,
                items=self._tar_pit_items(posts=1), origin="test")
        # Exactly the bound: the sixth media fetch is refused by the budget,
        # and the remaining items fail without any fetch attempt.
        self.assertEqual(len(attempts), 5)
        job = self._import_jobs()["https://example.com/post0"]
        self.assertIn("import item rejected: import fetch budget exhausted",
                      job.source["warnings"])

    def test_inactive_users_imports_stay_unclaimed(self):
        models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending"})
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(worker._claim_imports(), [])
        self.assertEqual(
            models.ImportRecord.objects.get().manifest["status"], "pending")

    def test_capture_runs_while_imports_hold_slots(self):
        """Two slow imports no longer occupy the whole pool: a queued capture
        job of another user is processed while the imports still run."""
        other = models.User.objects.create_user(username="w2", email="w2@example.com")
        other_personal = services.personal_collection(other)
        capture = models.Capture.objects.create(
            user=other, client_request_id=str(uuid.uuid4()),
            raw_text="https://example.com/capture", requested_collection_id=None,
            collection=other_personal, request_hash="h",
            received_at=timezone.now(), notice="")
        job = models.Job.objects.create(
            capture=capture, url="https://example.com/capture",
            state="queued", acquisition="pending", interpretation="pending",
            collection=other_personal, attempts=0)
        records = [models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending",
                      "collection_id": str(self.personal.id)})
            for _ in range(2)]
        gate = threading.Event()
        release = threading.Event()
        imports_run = []

        def slow_import(record, say=None):
            imports_run.append(record.id)
            gate.set()
            release.wait(10)
            # Mirror the real commit checkpoint: the engine's commit is the
            # one "done" writer; a fake engine must stamp it to complete.
            record.update_manifest({"status": models.ImportRecord.Status.DONE,
                                    "result": {"counts": {}}})
            return {"counts": {}}

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "_concurrency", lambda: 2), \
                mock.patch.object(worker.imports, "run_staged_import",
                                  side_effect=slow_import), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire), \
                mock.patch("clipshelf.interpretation.interpret",
                           side_effect=lambda source, config, cats, **kw:
                               {**FINDINGS, "source": source.get("url")}):
            thread = threading.Thread(target=worker.main, kwargs={"once": True},
                                      daemon=True)
            thread.start()
            self.assertTrue(gate.wait(5))
            done = False
            for _ in range(100):
                job.refresh_from_db()
                if job.state == "done":
                    done = True
                    break
                time.sleep(0.05)
            self.assertTrue(done, "capture must run while the import holds a slot")
            # The import share is capped: the second import waits until the
            # first releases its slot instead of squeezing out captures.
            self.assertEqual(len(imports_run), 1)
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive())
        for record in records:
            record.refresh_from_db()
            self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(len(imports_run), 2)


class AddExtractedCommandTests(WorkerMixin, TestCase):
    def test_rejects_malformed_nested_links_and_prompts_before_merge(self):
        path = self.tmp / "findings.json"
        malformed = (
            ("links", "null"), ("links", "[null]"), ("links", '[{"url": 1}]'),
            ("prompts", "null"), ("prompts", "[null]"), ("prompts", '[{"text": 1}]'),
        )
        for field, value in malformed:
            with self.subTest(field=field, value=value):
                path.write_text(f'{{"{field}": {value}}}', encoding="utf-8")
                with mock.patch(
                        "clipshelf.management.commands.add_extracted.imports.merge_findings"
                ) as merge_findings:
                    with self.assertRaisesMessage(CommandError, f"finding {field}"):
                        call_command("add_extracted", "--user", self.user.email, str(path))
                    merge_findings.assert_not_called()

    def test_rejects_wrong_skill_findings_shapes_before_merge(self):
        path = self.tmp / "findings.json"
        malformed = (
            ("categories", '["Video generation"]'),  # interpretation shape: a list
            ("categories", '{"owner/tool": ["Video generation"]}'),
            ("categories", "null"),
            ("installs", '["pip install tool"]'),
            ("repos", '{"owner/tool": "x"}'),
            ("repos", '["owner/tool", 3]'),
            ("source", "7"),
            ("summary", '["s"]'),
        )
        for field, value in malformed:
            with self.subTest(field=field, value=value):
                path.write_text(f'{{"{field}": {value}}}', encoding="utf-8")
                with mock.patch(
                        "clipshelf.management.commands.add_extracted.imports.merge_findings"
                ) as merge_findings:
                    with self.assertRaisesMessage(CommandError, f"finding {field}"):
                        call_command("add_extracted", "--user", self.user.email, str(path))
                    merge_findings.assert_not_called()

    def test_accepts_skill_shape_categories_and_installs(self):
        path = self.tmp / "findings.json"
        path.write_text(
            '{"source": "https://example.com/a", "summary": "s", '
            '"repos": ["owner/tool"], '
            '"categories": {"owner/tool": "Video generation"}, '
            '"installs": {"owner/tool": "pip install tool"}}', encoding="utf-8")
        with mock.patch(
                "clipshelf.management.commands.add_extracted.imports.merge_findings"
        ) as merge_findings:
            call_command("add_extracted", "--user", self.user.email, str(path))
        merge_findings.assert_called_once()

    def test_second_identical_run_reports_zero_changes(self):
        findings = [{
            "repos": ["owner/repo"],
            "prompts": [{"text": "use uv", "title": "uv", "cat": ""}],
        }]
        path = self.tmp / "findings.json"
        path.write_text(_json(findings), encoding="utf-8")
        first = io.StringIO()
        call_command("add_extracted", "--user", self.user.email, str(path), stdout=first)
        second = io.StringIO()
        call_command("add_extracted", "--user", self.user.email, str(path), stdout=second)
        self.assertIn("1 link change(s), 1 prompt change(s)", first.getvalue())
        self.assertIn("0 link change(s), 0 prompt change(s), 0 updated",
                      second.getvalue())


class PublicationIdentityTests(WorkerMixin, TransactionTestCase):
    """One publication owner: every route agrees on identity, removals hold."""

    def test_link_key_fallback_canonicalizes_urls_norm_rejects(self):
        from clipshelf import publication
        self.assertEqual(publication.link_key("https://user:pw@Example.com/a#frag"),
                         "https://example.com/a")
        self.assertEqual(publication.link_key("https://user@Example.com:8443/a"),
                         "https://example.com:8443/a")
        self.assertEqual(publication.link_key(" https:///no-host "), "https:///no-host")

    def test_redirect_keeps_source_and_findings_on_one_entry(self):
        job = self.make_job(url="https://example.com/short")
        resolved = "https://example.com/full-article"

        def redirecting(url, directory, status="complete"):
            source = fake_acquire(resolved, directory, status)
            source["original_url"] = url  # the captured identity, preserved
            return source

        self.run_worker(acquire=redirecting)
        self.refresh(job)
        self.assertEqual(job.final_url, resolved)
        self.assertEqual(job.source["original_url"], "https://example.com/short")
        entries = list(models.Entry.objects.filter(collection=self.personal, kind="link"))
        self.assertEqual([e.key for e in entries], [resolved])
        data = models.Contribution.objects.get(entry=entries[0]).data
        self.assertEqual(data["title"], "t")
        self.assertEqual(data["findings"]["summary"], "good findings")

    def test_tombstoned_redirect_publishes_nothing(self):
        original = "https://example.com/short"
        resolved = "https://example.com/full-article"
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=original)
        job = self.make_job(url=original)

        def redirecting(url, directory, status="complete"):
            source = fake_acquire(resolved, directory, status)
            source["original_url"] = url
            return source

        self.run_worker(
            acquire=redirecting,
            interpret=lambda source, config, cats, **kw: {
                **FINDINGS, "source": source.get("url"),
                "prompts": ["Summarize the changelog for operators."]})
        job = self.refresh(job)
        self.assertEqual(job.state, "done")
        self.assertEqual(job.interpretation, "complete")
        self.assertEqual(job.final_url, resolved)
        # The interpreter really produced a prompt, so no-publication is a real guard.
        self.assertTrue(job.findings["prompts"])
        # Neither the redirect target nor the tombstoned original may publish.
        self.assertFalse(models.Entry.objects.filter(collection=self.personal).exists())
        self.assertFalse(models.Contribution.objects.filter(job=job).exists())
        self.assertTrue(any("removed from this collection" in w for w in job.warnings),
                        job.warnings)

    def test_prompt_identity_reuses_legacy_entries_and_mints_sha256(self):
        from clipshelf import publication
        shipped = "Write a tidy release note."
        legacy = models.Entry.objects.create(
            collection=self.personal, kind="prompt", key=publication.prompt_key(shipped))
        fresh = "Summarize the changelog for operators."
        self.make_job()
        self.run_worker(interpret=lambda source, config, cats, **kw: {
            **FINDINGS, "source": source.get("url"), "prompts": [shipped, fresh]})
        keys = set(models.Entry.objects.filter(
            collection=self.personal, kind="prompt").values_list("key", flat=True))
        self.assertEqual(keys, {legacy.key, publication.prompt_digest(fresh)})
        self.assertEqual(
            models.Contribution.objects.get(entry=legacy).data["text"], shipped)

    def test_removed_prompt_never_returns_through_a_merge(self):
        from clipshelf import publication
        text = "A prompt the owner deleted."
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=publication.prompt_key(text))
        imports.merge_findings(
            self.user, [{"source": "https://example.com/a", "prompts": [{"text": text}]}])
        self.assertFalse(models.Entry.objects.filter(kind="prompt").exists())

    def test_removed_link_never_returns_through_an_import(self):
        url = "https://example.com/gone"
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=url)
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire):
            result = imports.import_items(
                user=self.user, collection_id=self.personal.id,
                items=[{"url": url, "title": "legacy"}])

        self.assertTrue(result["created"])
        self.assertFalse(models.Entry.objects.filter(key=url).exists())


class ImportDestinationRecheckTests(WorkerMixin, TransactionTestCase):
    """The import commit rechecks account and destination after staging:
    access lost during the long fetch never commits into the shared
    collection (the same rule store_source applies per job)."""

    def _import_to_shared(self, revoke):
        other = models.User.objects.create_user(username="w2", email="w2@example.com")
        shared = models.Collection.objects.create(name="s", kind="shared", owner=other)
        models.Membership.objects.create(collection=shared, user=self.user)
        real_stage = imports._stage_items

        def stage_then_revoke(items, cache_dir, staging):
            revoke(shared)
            return real_stage(items, cache_dir, staging)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire), \
                mock.patch.object(imports, "_stage_items", side_effect=stage_then_revoke):
            return shared, imports.import_items(
                user=self.user, collection_id=shared.id,
                items=[{"url": "https://example.com/late", "title": "legacy"}])

    def test_membership_lost_during_staging_lands_in_personal(self):
        def revoke(shared):
            models.Membership.objects.filter(collection=shared, user=self.user).delete()

        shared, result = self._import_to_shared(revoke)
        self.assertEqual(result["collection_id"], str(self.personal.id))
        self.assertIn("Personal collection", result["notice"])
        self.assertFalse(models.Entry.objects.filter(collection=shared).exists())
        self.assertFalse(models.Contribution.objects.filter(entry__collection=shared).exists())
        self.assertFalse(models.Capture.objects.filter(collection=shared).exists())
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, key="https://example.com/late").exists())

    def test_account_disabled_during_staging_fails_the_import(self):
        def revoke(shared):
            models.User.objects.filter(pk=self.user.pk).update(is_active=False)

        with self.assertRaises(PermissionDenied):
            self._import_to_shared(revoke)
        self.assertFalse(models.Entry.objects.exists())
        self.assertFalse(models.Contribution.objects.exists())
        self.assertFalse(models.Capture.objects.exists())


class GuardrailBlockedTests(WorkerMixin, TransactionTestCase):
    """Deterministic screening blocks: job blocked, zero attempts, no retry."""

    def test_interpret_guardrail_block(self):
        def blocked(source, config, cats, **kw):
            from clipshelf import interpretation
            raise interpretation.GuardrailBlocked("page steers the interpreter")

        job = self.make_job()
        self.run_worker(interpret=blocked)
        job = self.refresh(job)
        self.assertEqual(job.state, "blocked")
        self.assertEqual(job.interpretation, "blocked")
        self.assertIn("page steers the interpreter", job.error)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(worker._claim_batch(1), [])  # blocked is terminal

    def test_guardrail_retry_reruns_interpretation(self):
        def blocked(source, config, cats, **kw):
            from clipshelf import interpretation
            raise interpretation.GuardrailBlocked("page steers the interpreter")

        job = self.make_job()
        self.run_worker(interpret=blocked)
        job = self.refresh(job)
        job.requeue()
        calls = []

        def allowed(source, config, cats, **kw):
            calls.append(source["url"])
            return {**FINDINGS, "source": source["url"]}

        self.run_worker(interpret=allowed)
        self.refresh(job)
        self.assertEqual(calls, [job.url])
        self.assertEqual((job.state, job.interpretation), ("done", "complete"))
        self.assertEqual(job.findings["summary"], "good findings")


    def test_dangerous_findings_blocked_at_the_gate(self):
        job = self.make_job()
        with mock.patch("clipshelf.services.judgment.available", return_value=True), \
                mock.patch("clipshelf.services.judgment.screen_findings",
                           return_value={"danger": 2.5, "related": {}}):
            self.run_worker()
        job = self.refresh(job)
        self.assertEqual(job.state, "blocked")
        self.assertEqual(job.interpretation, "blocked")
        self.assertEqual(job.attempts, 0)
        self.assertTrue(job.error)
        self.assertEqual(worker._claim_batch(1), [])


class MergeTaggingTests(WorkerMixin, TransactionTestCase):
    """merge_findings tags: screened links, deterministic fallbacks."""

    def merge(self, links):
        imports.merge_findings(self.user, [{"links": links}])

    def tags(self, url):
        entry = models.Entry.objects.get(collection=self.personal, key=url)
        return models.Contribution.objects.get(entry=entry, user=self.user).data["tags"]

    def test_screening_key_reaches_judgment(self):
        key = mock.sentinel.screening_key
        links = [{"url": "https://example.com/tut-1", "title": "Nice Thing"}]
        with mock.patch("clipshelf.services.screening_key", return_value=key), \
                mock.patch("clipshelf.judgment.available", return_value=True) as available, \
                mock.patch("clipshelf.judgment.tag_links",
                           return_value=[{"keep": 0.9, "tag": "guide"}]) as tag_links:
            self.merge(links)
        available.assert_called_once_with(key)
        tag_links.assert_called_once()
        self.assertIs(tag_links.call_args.kwargs["api_key"], key)

    def test_screened_tags_replace_heuristics(self):
        links = [{"url": "https://example.com/tut-1", "title": "Nice Thing"},
                 {"url": "https://example.com/x-2", "title": "Other Thing"}]
        with mock.patch("clipshelf.judgment.available", return_value=True), \
                mock.patch("clipshelf.judgment.tag_links",
                           return_value=[{"keep": 0.9, "tag": "guide"},
                                         {"keep": 0.2, "tag": "docs"}]) as tag_links:
            self.merge(links)
        self.assertEqual(self.tags("https://example.com/tut-1"), ["guide"])
        self.assertEqual(self.tags("https://example.com/x-2"), ["page"])
        self.assertEqual(tag_links.call_args.args[0], [
            {"url": "https://example.com/tut-1", "title": "Nice Thing"},
            {"url": "https://example.com/x-2", "title": "Other Thing"}])

    def test_unavailable_falls_back_to_lib_tags(self):
        with mock.patch("clipshelf.judgment.available", return_value=False):
            self.merge([{"url": "https://example.com/pp-1",
                         "title": "The prompt collection"}])
        self.assertEqual(self.tags("https://example.com/pp-1"), ["prompt"])

    def test_structural_github_tag_never_hits_the_api(self):
        with mock.patch("clipshelf.judgment.available", return_value=True), \
                mock.patch("clipshelf.judgment.tag_links",
                           return_value=[{"keep": 0.9, "tag": "guide"}]) as tag_links:
            self.merge([{"url": "https://github.com/owner/repo", "title": "Repo"},
                        {"url": "https://example.com/tut-1", "title": "Nice Thing"}])
        tag_links.assert_called_once()
        self.assertEqual(tag_links.call_args.args[0],
                         [{"url": "https://example.com/tut-1", "title": "Nice Thing"}])
        self.assertEqual(self.tags("https://github.com/owner/repo"), ["github"])


class StagingRetentionTests(WorkerMixin, TestCase):
    """Review S12: staged uploads are owned by their ImportRecord, kept past
    completion, and only finished uploads past the window are ever named as
    cleanup candidates — never live or unrelated files."""

    def stage(self, status="pending", rel=None, extra=None):
        rel = rel or f"staging/import-{uuid.uuid4().hex}.json"
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[{"url": "https://example.com/a"}]', encoding="utf-8")
        manifest = {"status": status, "staging": rel,
                    "collection_id": str(self.personal.id), "format": "tiktok",
                    "client_request_id": str(uuid.uuid4())}
        manifest.update(extra or {})
        record = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex, manifest=manifest)
        return path, record

    def candidates(self, days):
        with override_settings(DATA_DIR=str(self.tmp)):
            return worker.staging_cleanup_candidates(
                now=timezone.now() + timedelta(days=days))

    def test_finished_imports_expire_only_after_the_window(self):
        fresh_path, _ = self.stage(
            status="done", extra={"completed_at": timezone.now().isoformat()})
        old = (timezone.now() - timedelta(days=60)).isoformat()
        failed_path, _ = self.stage(status="error", extra={"completed_at": old})
        self.assertEqual(self.candidates(0), [failed_path.resolve()])
        self.assertEqual(self.candidates(31),
                         sorted([fresh_path.resolve(), failed_path.resolve()]))

    def test_run_import_stamps_completion_and_retains_the_upload(self):
        path, record = self.stage()
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.import_export",
                           side_effect=lambda batch, directory, budget=None:
                               [fake_acquire(item["url"], directory) for item in batch]):
            worker._run_import(record)
        record.refresh_from_db()
        # The commit checkpoint stamps completion, atomic with its rows; the
        # worker adds no second "done" write.
        self.assertEqual(record.manifest["status"], models.ImportRecord.Status.DONE)
        self.assertTrue(record.manifest["completed_at"])
        self.assertEqual(record.manifest["result"]["counts"]["entries"], 1)
        self.assertTrue(path.is_file())
        self.assertEqual(self.candidates(0), [])
        self.assertEqual(self.candidates(31), [path.resolve()])

    def test_failed_import_stamps_error_and_retains_the_upload(self):
        path, record = self.stage()
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.imports.import_items",
                           side_effect=ValidationError("boom")):
            worker._run_import(record)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "error")
        self.assertIn("boom", record.manifest["error"])
        self.assertTrue(record.manifest["completed_at"])
        self.assertTrue(path.is_file())
        self.assertEqual(self.candidates(31), [path.resolve()])

    def test_live_records_keep_their_uploads_ineligible(self):
        old = (timezone.now() - timedelta(days=60)).isoformat()
        self.stage(status="pending", extra={"completed_at": old})
        self.stage(status="running", extra={"completed_at": old})
        self.assertEqual(self.candidates(3650), [])
        # A finished record and a live record naming the same file: live wins.
        shared_rel = f"staging/import-{uuid.uuid4().hex}.json"
        shared_path, _ = self.stage(status="done", rel=shared_rel,
                                    extra={"completed_at": old})
        contender = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending", "staging": shared_rel})
        self.assertEqual(self.candidates(3650), [])
        contender.manifest = dict(contender.manifest, status="done")
        contender.save(update_fields=["manifest"])
        self.assertEqual(self.candidates(3650), [shared_path.resolve()])

    def test_unrelated_and_misowned_paths_are_never_candidates(self):
        old = (timezone.now() - timedelta(days=60)).isoformat()
        staging = self.tmp / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "import-stray.json").write_text("[]", encoding="utf-8")
        (staging / "job-7").mkdir(parents=True, exist_ok=True)
        media = staging / "import-media" / "x.png"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"\x89PNG")
        # No record claims the stray file or the directories.
        self.assertEqual(self.candidates(3650), [])
        # Records whose staging value is not a staged upload file under
        # DATA_DIR/staging are skipped: outside the root, a directory, absent.
        self.stage(status="done", rel="cache/evil.json",
                   extra={"completed_at": old})
        models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "done", "staging": "staging/import-media",
                      "completed_at": old})
        models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "done", "staging": "staging/import-gone.json",
                      "completed_at": old})
        models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "done", "completed_at": old})
        self.assertEqual(self.candidates(3650), [])


def _json(obj):
    import json
    return json.dumps(obj)


class StagingCleanupTests(WorkerMixin, TestCase):
    """Review follow-up: rejected/unpublished staging bytes stay owned and
    retained, and only terminal owners past the window are ever cleanup
    candidates — never live work or paths no row names."""

    def job_dir(self, job):
        path = Path(self.tmp) / "staging" / f"job-{job.id}"
        path.mkdir(parents=True, exist_ok=True)
        (path / "video.mp4").write_bytes(b"\0" * 32)
        return path.resolve()

    def media_dir(self):
        rel = f"staging/import-{uuid.uuid4().hex}"
        path = self.tmp / rel
        path.mkdir(parents=True, exist_ok=True)
        (path / "video.mp4").write_bytes(b"\0" * 32)
        return rel, path.resolve()

    def record(self, rel, status, completed_at=None):
        manifest = {"media_staging": rel, "status": status}
        if completed_at is not None:
            manifest["completed_at"] = completed_at
        return models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex, manifest=manifest)

    def backdate(self, job, days=60):
        models.Job.objects.filter(pk=job.pk).update(
            updated_at=timezone.now() - timedelta(days=days))

    def candidates(self, days):
        now = timezone.now() + timedelta(days=days)
        with override_settings(DATA_DIR=str(self.tmp)):
            return (worker._job_staging_cleanup_candidates(now=now),
                    worker._import_media_cleanup_candidates(now=now))

    def test_job_dirs_expire_only_after_the_terminal_window(self):
        active = self.make_job(state="queued")
        self.backdate(active)
        fresh = self.make_job(state="blocked")
        old_blocked = self.make_job(state="blocked")
        self.backdate(old_blocked)
        old_done = self.make_job(state="done")
        self.backdate(old_done)
        dirs = {job.id: self.job_dir(job)
                for job in (active, fresh, old_blocked, old_done)}
        # 60-day-old terminal dirs pass the 30-day window; the active job and
        # the fresh blocked job stay ineligible.
        self.assertEqual(self.candidates(0)[0],
                         sorted([dirs[old_blocked.id], dirs[old_done.id]]))
        self.assertEqual(self.candidates(31)[0],
                         sorted([dirs[fresh.id], dirs[old_blocked.id],
                                 dirs[old_done.id]]))

    def test_unowned_and_misplaced_job_paths_are_never_candidates(self):
        old = self.make_job(state="blocked")
        self.backdate(old)
        # A directory no Job row names (stray or crash leftover) is unreachable.
        stray = Path(self.tmp) / "staging" / f"job-{uuid.uuid4()}"
        stray.mkdir(parents=True)
        self.assertEqual(self.candidates(3650)[0], [])
        # Terminal job, but nothing (or a plain file) at its staging path.
        (Path(self.tmp) / "staging" / f"job-{old.id}").write_text("x")
        self.assertEqual(self.candidates(3650)[0], [])
        models.Job.objects.filter(pk=old.pk).update(state="done")
        self.assertEqual(self.candidates(3650)[0], [])

    def test_media_dirs_expire_only_after_the_terminal_window(self):
        live_rel, live_path = self.media_dir()
        self.record(live_rel, "pending",
                    completed_at=(timezone.now() - timedelta(days=60)).isoformat())
        old = (timezone.now() - timedelta(days=60)).isoformat()
        failed_rel, failed_path = self.media_dir()
        self.record(failed_rel, "error", completed_at=old)
        fresh_rel, fresh_path = self.media_dir()
        self.record(fresh_rel, "done", completed_at=timezone.now().isoformat())
        self.assertEqual(self.candidates(0)[1], [failed_path])
        self.assertEqual(self.candidates(31)[1], sorted([failed_path, fresh_path]))
        # A live (pending) owner keeps its directory ineligible at any age.
        self.assertNotIn(live_path, self.candidates(3650)[1])

    def test_live_owner_keeps_a_shared_media_dir_ineligible(self):
        old = (timezone.now() - timedelta(days=60)).isoformat()
        shared_rel, shared_path = self.media_dir()
        self.record(shared_rel, "done", completed_at=old)
        contender = self.record(shared_rel, "pending")
        self.assertEqual(self.candidates(3650)[1], [])
        contender.manifest = dict(contender.manifest, status="done")
        contender.save(update_fields=["manifest"])
        self.assertEqual(self.candidates(3650)[1], [shared_path])

    def test_import_ownership_survives_a_crashed_media_directory(self):
        record = models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"status": "pending"})
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(imports, "_stage_items",
                                  side_effect=ValidationError("boom")):
            with self.assertRaises(ValidationError):
                imports.import_items(user=self.user, collection_id=self.personal.id,
                                    record=record,
                                    items=[{"url": "https://example.com/a"}])
        record.refresh_from_db()
        # The staged work failed, but the directory was owned by name before
        # any work ran and the finally-cleanup removed it; recreating the
        # crash state makes it a candidate only once finished and expired.
        rel = record.manifest["media_staging"]
        self.assertTrue(rel.startswith("staging/import-"))
        self.assertFalse((self.tmp / rel).exists())
        (self.tmp / rel).mkdir(parents=True)
        self.assertEqual(self.candidates(3650)[1], [])
        record.manifest = dict(record.manifest, status="error",
                               completed_at=(timezone.now() - timedelta(days=60))
                               .isoformat())
        record.save(update_fields=["manifest"])
        self.assertEqual(self.candidates(3650)[1], [(self.tmp / rel).resolve()])

    def test_clean_command_lists_then_deletes_only_candidates(self):
        old_blocked = self.make_job(state="blocked")
        self.backdate(old_blocked)
        expired = self.job_dir(old_blocked)
        live = self.job_dir(self.make_job(state="running"))
        live_rel, live_media = self.media_dir()
        self.record(live_rel, "pending")
        spool_rel = f"staging/import-{uuid.uuid4().hex}.json"
        spool = self.tmp / spool_rel
        spool.write_text('[{"url": "https://example.com/s"}]', encoding="utf-8")
        models.ImportRecord.objects.create(
            user=self.user, input_digest=uuid.uuid4().hex,
            manifest={"staging": spool_rel, "status": "done",
                      "completed_at": (timezone.now() - timedelta(days=60))
                      .isoformat()})
        stray = Path(self.tmp) / "staging" / "import-stray.json"
        stray.write_text("[]", encoding="utf-8")

        listed = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)):
            call_command("clean", stdout=listed)
        self.assertIn(str(expired), listed.getvalue())
        self.assertIn(str(spool), listed.getvalue())
        self.assertIn("dry run", listed.getvalue())
        self.assertTrue(expired.exists())
        self.assertTrue(spool.exists())

        executed = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)):
            call_command("clean", "--execute", stdout=executed)
        self.assertIn(f"deleted  {expired}", executed.getvalue())
        self.assertIn(f"deleted  {spool}", executed.getvalue())
        self.assertFalse(expired.exists())
        self.assertFalse(spool.exists())
        # Live work, its media dir, and unowned paths are untouched.
        self.assertTrue(live.exists())
        self.assertTrue(live_media.exists())
        self.assertTrue(stray.exists())

    def test_cli_clean_previews_then_executes_expired_staging(self):
        old = self.make_job(state="blocked")
        self.backdate(old)
        expired = self.job_dir(old)
        output = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)), redirect_stdout(output):
            cli.main(["clean"])
        self.assertIn("dry run: nothing deleted", output.getvalue())
        self.assertTrue(expired.exists())

        output = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)), redirect_stdout(output):
            cli.main(["clean", "--execute"])
        self.assertIn(f"deleted  {expired}", output.getvalue())
        self.assertFalse(expired.exists())

    def test_cli_never_wraps_a_lock_owning_command_in_the_shared_lock(self):
        """v0.10.0 regression: cli.main held the shared data lock while a
        command took the exclusive one, so `python clipshelf.py <cmd>` died
        on the nesting RuntimeError. Ownership lives on the Command class,
        so a new exclusive-lock command is covered without touching cli.py."""
        import inspect
        from django.core.management import get_commands, load_command_class

        owners = set()
        for name, app in get_commands().items():
            cmd = load_command_class(app, name)
            exclusive = "data_lock(exclusive=True" in inspect.getsource(type(cmd))
            self.assertEqual(exclusive, getattr(cmd, "owns_data_lock", False),
                             f"{name}: exclusive-lock commands declare owns_data_lock")
            if exclusive:
                owners.add(name)
        self.assertEqual(owners, {"backup", "restore", "clean"})
        for name in sorted(owners):
            with override_settings(DATA_DIR=str(self.tmp)), \
                    mock.patch("clipshelf.management.locks.data_lock",
                               side_effect=AssertionError(f"cli.main locked around {name}")), \
                    mock.patch("django.core.management.execute_from_command_line"):
                cli.main([name])

    def test_recovered_import_retains_previous_media_dirs(self):
        """A crash retry must not orphan the first crash's staged directory:
        the superseded rel moves to retained_media_staging and every named dir
        becomes a cleanup candidate once the record is terminal and expired."""
        old_rel, old_path = self.media_dir()
        record = self.record(old_rel, "pending")
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(imports, "_stage_items",
                                  side_effect=ValidationError("boom")):
            with self.assertRaises(ValidationError):
                imports.import_items(user=self.user, collection_id=self.personal.id,
                                    record=record,
                                    items=[{"url": "https://example.com/a"}])
            record.refresh_from_db()
            mid_rel = record.manifest["media_staging"]
            self.assertNotEqual(mid_rel, old_rel)
            self.assertEqual(record.manifest["retained_media_staging"], [old_rel])
            # Second crash: the mid rel is superseded the same way.
            record.manifest = dict(record.manifest, status="pending")
            record.save(update_fields=["manifest"])
            with self.assertRaises(ValidationError):
                imports.import_items(user=self.user, collection_id=self.personal.id,
                                    record=record,
                                    items=[{"url": "https://example.com/a"}])
        record.refresh_from_db()
        self.assertEqual(record.manifest["retained_media_staging"],
                         sorted([old_rel, mid_rel]))
        # The finally-cleanup of the failed runs removed all named dirs;
        # recreate the crash leftovers on disk.
        for rel in [record.manifest["media_staging"], old_rel, mid_rel]:
            (self.tmp / rel).mkdir(parents=True, exist_ok=True)
            (self.tmp / rel / "video.mp4").write_bytes(b"\0" * 32)
        paths = sorted((self.tmp / rel).resolve()
                       for rel in [record.manifest["media_staging"], old_rel, mid_rel])
        # Terminal + expired: all three named dirs are candidates.
        record.manifest = dict(record.manifest, status="error",
                               completed_at=(timezone.now() - timedelta(days=60))
                               .isoformat())
        record.save(update_fields=["manifest"])
        self.assertEqual(self.candidates(3650)[1], paths)
        # A live owner keeps every named dir ineligible.
        record.manifest = dict(record.manifest, status="running")
        record.save(update_fields=["manifest"])
        self.assertEqual(self.candidates(3650)[1], [])

    def test_execute_skips_paths_reactivated_after_listing(self):
        """--execute rechecks each listed path under the exclusive lock; a job
        requeued between listing and deletion keeps its staging directory."""
        old_blocked = self.make_job(state="blocked")
        self.backdate(old_blocked)
        expired = self.job_dir(old_blocked)
        keep = self.make_job(state="blocked")
        self.backdate(keep)
        kept_dir = self.job_dir(keep)

        real = worker._job_staging_cleanup_candidates

        def stale_listing(now=None):
            paths = real(now=now)
            # Reactivate after the snapshot, as a concurrent worker would.
            models.Job.objects.filter(pk=old_blocked.pk).update(state="queued")
            return paths

        listed = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "_job_staging_cleanup_candidates",
                                  stale_listing):
            call_command("clean", "--execute", stdout=listed)
        self.assertTrue(expired.exists())
        self.assertFalse(kept_dir.exists())
        self.assertIn("skipped", listed.getvalue())
        self.assertIn(str(expired), listed.getvalue())


class _Job:
    def __init__(self, tag):
        self.tag = tag


class CoordinatorRefillTests(WorkerMixin, TransactionTestCase):
    """The coordinator refills a worker slot as soon as any future completes;
    once-mode keeps claiming until nothing is claimable, then drains."""

    def test_refills_a_free_slot_on_first_completion(self):
        calls = {"imports": 0}
        lock = threading.Lock()
        started = []
        events = {"long": threading.Event(), "refill": threading.Event(),
                  "short": threading.Event()}
        release = threading.Event()

        def fake_claims(limit):
            order = [["long", "short"], ["refill"], []]
            idx = calls.setdefault("claims", 0)
            calls["claims"] = idx + 1
            return [_Job(tag) for tag in order[idx]] if idx < 3 else []

        def fake_process(job):
            events[job.tag].set()
            if job.tag == "long":
                release.wait(10)
            with lock:
                started.append(job.tag)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "_concurrency", lambda: 2), \
                mock.patch.object(worker, "_claim_imports",
                                  side_effect=lambda limit: calls.__setitem__(
                                      "imports", calls["imports"] + 1) or []), \
                mock.patch.object(worker, "_run_import",
                                  side_effect=AssertionError("no imports")), \
                mock.patch.object(worker, "_claim_batch", side_effect=fake_claims), \
                mock.patch.object(worker, "_process", side_effect=fake_process):
            t = threading.Thread(target=worker.main, kwargs={"once": True},
                                 daemon=True)
            t.start()
            # refill must start while long still holds its slot.
            self.assertTrue(events["refill"].wait(5))
            self.assertFalse(release.is_set())
            release.set()
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(started, ["short", "refill", "long"])
        # Top-up rounds re-claim imports too: startup plus every refill
        # while a slot is free, not a single startup claim.
        self.assertGreaterEqual(calls["imports"], 2)

    def test_once_mode_drains_all_claimable_work_before_breaking(self):
        claims = {"n": 0}
        processed = []
        batches = [["a"], ["b"], []]
        lock = threading.Lock()

        def fake_claims(limit):
            n = claims["n"]
            claims["n"] = n + 1
            return [_Job(tag) for tag in batches[n]] if n < 3 else []

        def fake_process(job):
            with lock:
                processed.append(job.tag)

        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "_concurrency", lambda: 2), \
                mock.patch.object(worker, "_claim_imports", side_effect=lambda limit: []), \
                mock.patch.object(worker, "_run_import",
                                  side_effect=AssertionError("no imports")), \
                mock.patch.object(worker, "_claim_batch", side_effect=fake_claims), \
                mock.patch.object(worker, "_process", side_effect=fake_process):
            t = threading.Thread(target=worker.main, kwargs={"once": True},
                                 daemon=True)
            t.start()
            t.join(10)
            self.assertFalse(t.is_alive())
        self.assertEqual(sorted(processed), ["a", "b"])
        # The third, empty claim round proves it kept claiming after work
        # remained claimable, then broke once nothing was claimable.
        self.assertEqual(claims["n"], 3)


if __name__ == "__main__":
    from django.conf import settings
    from django.test.utils import get_runner
    raise SystemExit(bool(get_runner(settings)().run_tests(["test_worker"])))
