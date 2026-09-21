"""Worker coordinator regressions: claims, recovery, pauses, finite failures."""
import io
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from clipshelf import models, services, worker

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
        side = interpret or (lambda source, config, cats: {**FINDINGS, "source": source.get("url")})
        with override_settings(DATA_DIR=str(self.tmp)), \
                mock.patch("clipshelf.acquisition.acquire", side_effect=acquire), \
                mock.patch("clipshelf.interpretation.interpret", side_effect=side):
            worker.main(once=True)

    def refresh(self, job):
        job.refresh_from_db()
        return job


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

    def test_interpretation_failure_preserves_last_good_findings(self):
        job = self.make_job()
        self.run_worker()
        self.assertEqual(self.refresh(job).findings["summary"], "good findings")
        job.state = "queued"  # reprocess: pipeline must keep the last good findings
        job.interpretation = "pending"
        job.save()

        def boom(source, config, cats):
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

        def revoked(source, config, cats):
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
            for attempt in range(1, worker.MAX_ATTEMPTS + 1):
                claimed = worker._claim_batch(1)
                self.assertEqual([j.pk for j in claimed], [job.pk])
                worker._process(claimed[0])
                self.refresh(job)
                self.assertEqual(upstream.call_count, attempt)
                self.assertEqual(job.attempts, attempt)
                self.assertIn("429", job.error)
                self.assertNotIn(cfg.llm_api_key, job.error)
                self.assertEqual(worker._claim_batch(1), [])
                if attempt < worker.MAX_ATTEMPTS:
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


def settings_path(rel):
    from django.conf import settings
    return Path(settings.DATA_DIR) / rel


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
        self.run_worker(interpret=lambda source, config, cats: {
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
        with override_settings(DATA_DIR=str(self.tmp)):
            result = worker.import_items(
                user=self.user, collection_id=self.personal.id,
                items=[{"url": url, "title": "legacy"}])
        self.assertTrue(result["created"])
        self.assertFalse(models.Entry.objects.filter(key=url).exists())


class GuardrailBlockedTests(WorkerMixin, TransactionTestCase):
    """Deterministic screening blocks: job blocked, zero attempts, no retry."""

    def test_interpret_guardrail_block(self):
        def blocked(source, config, cats):
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


def _json(obj):
    import json
    return json.dumps(obj)


def _ingest(path):
    import io
    import json as jsonlib
    out = io.StringIO()
    call_command("ingest", "--user", "w1@example.com", str(path), stdout=out)
    return jsonlib.loads(out.getvalue())
