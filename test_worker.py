"""Worker coordinator regressions: claims, recovery, pauses, finite failures."""
from contextlib import redirect_stdout
import io
import shutil
import threading
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from clipshelf import cli, models, services, worker

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

    def test_mark_done_defaults_and_withheld_variant(self):
        job = self.make_job(state="running")
        job.mark_done()
        self.assertEqual(
            (job.state, job.error, job.interpretation),
            ("done", "", "complete"))
        job.mark_done(interpretation="blocked",
                      warnings=["terminal host: retained as metadata only, never interpreted"])
        self.assertEqual(
            (job.state, job.interpretation, job.warnings),
            ("done", "blocked", ["terminal host: retained as metadata only, never interpreted"]))


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

    def test_abandoned_running_recovered(self):
        job = self.make_job(state="running")
        self.run_worker()
        self.assertEqual(self.refresh(job).state, "done")

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
        items = [{"url": "https://example.com/imported", "title": "From browser"}]
        with override_settings(DATA_DIR=str(self.tmp)):
            (Path(self.tmp) / "staging").mkdir(parents=True, exist_ok=True)
            (Path(self.tmp) / staging_rel).write_text(_json(items), encoding="utf-8")

        def fake_import_export(batch, directory):
            out = []
            for item in batch:
                p = Path(directory) / "i.html"
                p.write_text("bytes", encoding="utf-8")
                out.append({"url": item["url"], "original_url": item["url"], "title": "",
                            "desc": "", "text": "", "links": [],
                            "assets": [{"path": str(p), "kind": "page",
                                        "content_type": "text/html", "position": 0}],
                            "acquisition": "complete", "warnings": [], "metadata": {}})
            return out

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

        with mock.patch("clipshelf.acquisition.import_export", side_effect=fake_import_export):
            self.run_worker(acquire=blocked_acquire)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertEqual(record.manifest["result"]["counts"]["entries"], 1)
        self.assertTrue(models.Entry.objects.filter(
            collection=self.personal, kind="link",
            key="https://example.com/imported").exists())
        # Imported material is the acquisition: the worker interprets it and
        # never silently re-fetches the gated post from upstream.
        self.assertEqual(upstream, [])
        job = models.Job.objects.filter(url="https://example.com/imported").get()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.acquisition, "complete")
        self.assertEqual(job.interpretation, "complete")
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
                        "clipshelf.management.commands.add_extracted.worker.merge_findings"
                ) as merge_findings:
                    with self.assertRaisesMessage(CommandError, f"finding {field}"):
                        call_command("add_extracted", "--user", self.user.email, str(path))
                    merge_findings.assert_not_called()

    def test_reports_changes_without_calling_them_new_items(self):
        path = self.tmp / "findings.json"
        path.write_text("{}", encoding="utf-8")
        output = io.StringIO()
        with mock.patch(
                "clipshelf.management.commands.add_extracted.worker.merge_findings",
                return_value={"links": 2, "prompts": 3, "updated": 4},
        ) as merge_findings:
            call_command("add_extracted", "--user", self.user.email, str(path), stdout=output)
        merge_findings.assert_called_once()
        self.assertIn("2 link change(s)", output.getvalue())
        self.assertIn("3 prompt change(s)", output.getvalue())
        self.assertIn("4 updated", output.getvalue())
        self.assertNotIn("new link", output.getvalue())
        self.assertNotIn("new prompt", output.getvalue())


class IngestCommandTests(WorkerMixin, TestCase):
    def test_ingest_idempotent_receipt(self):
        dump = self.tmp / "dump.txt"
        dump.write_text("read https://example.com/x later\n", encoding="utf-8")
        first = _ingest(dump)
        self.assertTrue(first["created"])
        second = _ingest(dump)
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"], "identical file must reuse the receipt")
        self.assertEqual(models.Capture.objects.filter(user=self.user).count(), 1)


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
        worker.merge_findings(
            self.user, [{"source": "https://example.com/a", "prompts": [{"text": text}]}])
        self.assertFalse(models.Entry.objects.filter(kind="prompt").exists())

    def test_removed_link_never_returns_through_an_import(self):
        url = "https://example.com/gone"
        models.History.objects.create(
            collection=self.personal, user=self.user,
            kind=models.History.Kind.REMOVED, url=url)
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=fake_acquire):
            result = worker.import_items(
                user=self.user, collection_id=self.personal.id,
                items=[{"url": url, "title": "legacy"}])

        self.assertTrue(result["created"])
        self.assertFalse(models.Entry.objects.filter(key=url).exists())


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
        worker.merge_findings(self.user, [{"links": links}])

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
                mock.patch("clipshelf.worker.import_items",
                           return_value={"counts": {}}):
            worker._run_import(record)
        record.refresh_from_db()
        self.assertEqual(record.manifest["status"], "done")
        self.assertTrue(record.manifest["completed_at"])
        self.assertTrue(path.is_file())
        self.assertEqual(self.candidates(0), [])
        self.assertEqual(self.candidates(31), [path.resolve()])

    def test_failed_import_stamps_error_and_retains_the_upload(self):
        path, record = self.stage()
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.worker.import_items",
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


def _ingest(path):
    import io
    import json as jsonlib
    out = io.StringIO()
    call_command("ingest", "--user", "w1@example.com", str(path), stdout=out)
    return jsonlib.loads(out.getvalue())


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
            return (worker.job_staging_cleanup_candidates(now=now),
                    worker.import_media_cleanup_candidates(now=now))

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
                mock.patch.object(worker, "_stage_items",
                                  side_effect=ValidationError("boom")):
            with self.assertRaises(ValidationError):
                worker.import_items(user=self.user, collection_id=self.personal.id,
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

    def test_recovered_import_retains_previous_media_dirs(self):
        """A crash retry must not orphan the first crash's staged directory:
        the superseded rel moves to retained_media_staging and every named dir
        becomes a cleanup candidate once the record is terminal and expired."""
        old_rel, old_path = self.media_dir()
        record = self.record(old_rel, "pending")
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "_stage_items",
                                  side_effect=ValidationError("boom")):
            with self.assertRaises(ValidationError):
                worker.import_items(user=self.user, collection_id=self.personal.id,
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
                worker.import_items(user=self.user, collection_id=self.personal.id,
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

        real = worker.job_staging_cleanup_candidates

        def stale_listing(now=None):
            paths = real(now=now)
            # Reactivate after the snapshot, as a concurrent worker would.
            models.Job.objects.filter(pk=old_blocked.pk).update(state="queued")
            return paths

        listed = io.StringIO()
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch.object(worker, "job_staging_cleanup_candidates",
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
                                  side_effect=lambda: calls.__setitem__(
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
                mock.patch.object(worker, "_claim_imports", side_effect=lambda: []), \
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
